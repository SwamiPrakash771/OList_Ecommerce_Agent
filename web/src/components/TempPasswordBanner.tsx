// "Temporary password" reminder. Each account can dismiss it; the choice is
// remembered in this browser per username, so other accounts still see theirs.

import { X } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useStore } from '../state'

const key = (username: string) => `olist:pw-banner-dismissed:${username}`

function isDismissed(username: string) {
  try {
    return localStorage.getItem(key(username)) === '1'
  } catch {
    return false
  }
}

export default function TempPasswordBanner({ accountPath, className = '' }: { accountPath: string; className?: string }) {
  const { user } = useStore()
  const [dismissed, setDismissed] = useState<string | null>(null)
  if (!user?.must_change_password || dismissed === user.username || isDismissed(user.username)) return null

  const dismiss = () => {
    try {
      localStorage.setItem(key(user.username), '1')
    } catch {
      /* storage blocked: hide for this visit only */
    }
    setDismissed(user.username)
  }

  return (
    <div className={`relative bg-amber-50 py-2 pl-4 pr-12 text-center text-sm text-amber-800 ${className}`} role="status">
      You're using a temporary password. <Link to={accountPath} className="font-semibold underline">Change it now</Link>
      <button
        onClick={dismiss}
        className="absolute right-3 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-lg text-amber-700 hover:bg-amber-100"
        aria-label="Dismiss temporary password reminder"
        title="Dismiss"
      >
        <X size={16} />
      </button>
    </div>
  )
}
