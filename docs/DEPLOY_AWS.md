# Deploying to AWS (Lightsail containers + RDS)

Status: the database (RDS) exists and holds the data. The pipeline and the container service are being
set up; until they are, the live site still runs on the single Lightsail server.

## The idea

```
git push ──► CodeCommit ──► CodePipeline
                              ├─ Build  (CodeBuild, buildspec.yml):  tests ─► Docker image ─► ECR
                              └─ Deploy (CodeBuild, deploy/buildspec-deploy.yml):
                                    reads the settings NOW from Secrets Manager + Parameter Store
                                    ─► Lightsail container service: "api" + "scheduler" containers
                                                                         │
                                                                         ▼
                                                                 RDS MySQL (olist-db)
```

**Secrets are never in git or in the image.** The image is the same everywhere; a container gets its
settings as environment variables when it starts. Locally, `.env` provides the same names.

## Where everything is (AWS console, region Ohio / us-east-2)

| What | Where |
|---|---|
| Code | CodeCommit, repository `olist` |
| Images (one per commit) | ECR, repository `olist` |
| Pipeline and build logs | CodePipeline `olist`, CodeBuild |
| Running app | Lightsail, Containers, service `olist` (deployments, logs, URL) |
| Database | RDS, database `olist-db` (MySQL 8.4) |
| Database firewall | EC2, Security groups, `olist-db`: only listed IPs can connect |
| Passwords and keys | Secrets Manager: `olist/prod` (the app), `olist/db-logins` (your logins), and the RDS master password (`rds!db-...`, managed by RDS) |
| Plain settings | Systems Manager, Parameter Store, names starting `/olist/prod/` |

## Settings: which go where

The names are the ones in `.env.example`.

| Secrets Manager `olist/prod` (JSON key/values) | Parameter Store `/olist/prod/<NAME>` |
|---|---|
| `MYSQL_PASSWORD` | `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER` |
| `AGENT_MYSQL_PASSWORD`, `SELLER_AGENT_MYSQL_PASSWORD`, `ADMIN_AGENT_MYSQL_PASSWORD` | `AGENT_MYSQL_USER`, `SELLER_AGENT_MYSQL_USER`, `ADMIN_AGENT_MYSQL_USER` |
| `JWT_SECRET` | `MYSQL_SSL_CA` = `/etc/ssl/rds/global-bundle.pem` |
| `GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` | `APP_ENV` = `production`, `COOKIE_SECURE` = `true` |
| `ADMIN_PASSWORD`, `CUSTOMER_DEFAULT_PASSWORD`, `SELLER_DEFAULT_PASSWORD` | `DEMO_LOGIN`, `AI_MODEL`, `AI_LOCAL_EMBEDDINGS_ENABLED` = `false` |

## Everyday tasks

**Ship a code change.** `git push`. The pipeline tests, builds and deploys (3 to 5 minutes).
`https://<site>/api/health` shows the commit that is live (`"version"`). If the tests fail, nothing is
deployed. If the new version fails its health check, the old one keeps running.

**Change a password, key or setting.** No code change and no rebuild:
1. Secrets Manager, `olist/prod`, *Retrieve secret value*, *Edit*, change the value, *Save*
   (or Parameter Store, the `/olist/prod/...` parameter, *Edit*).
2. CodePipeline, `olist`, *Release change*. The deploy step reads the new values and starts new
   containers.

**Roll back.** Lightsail, Containers, `olist`, *Deployments*: pick an earlier deployment and redeploy
it. Or revert the commit in git and push.

**Look at or edit the data.** See the next section. Take a snapshot before editing
(RDS, `olist-db`, *Actions*, *Take snapshot*).

## Connecting to the database (MySQL Workbench, DBeaver or VS Code)

1. Get your login: Secrets Manager, `olist/db-logins`, *Retrieve secret value*. It holds the host,
   the port and two logins:
   - `olist_viewer`: read only. Use this one normally.
   - `olist_editor`: read and write. Use it only when you mean to change data.
2. Download Amazon's certificate bundle: https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
3. In your tool, create a MySQL connection with that host, port 3306, the user and password, database
   `olist`, and **SSL required**, with the bundle as the *CA file*. Connections without SSL are refused.
   - Workbench: *SSL* tab, *Use SSL* = Require, *SSL CA File* = the bundle.
   - VS Code: the "MySQL Shell for VS Code" or "SQLTools" extension, with the same values.
4. Only allowed IP addresses can connect (security group `olist-db`). If your home IP changes, update
   the rule: EC2, Security groups, `olist-db`, *Edit inbound rules*.

Editing data directly skips the app's own rules. For example, changing an order's status by hand creates
no refund rows. Prefer the admin screens for business changes.

## Safety checks in the repo

- `.githooks/pre-commit` runs `scripts/check_secrets.py` and blocks a commit that contains keys, passwords
  or `.env` files. Turn it on once per clone: `git config core.hooksPath .githooks`.
- `.dockerignore` keeps `.env`, keys, data files and tests out of the image.

## Stopping the costs

Delete in this order: the CodePipeline pipeline, the Lightsail container service, the RDS database
(first turn off *Deletion protection*, and keep a final snapshot if you want one), the ECR repository, the
secrets (`olist/prod`, `olist/db-logins`) and the security group `olist-db`. The RDS master secret is
removed with the database.
