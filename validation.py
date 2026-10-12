"""Input validation shared by the web API and the AI agent tools (one set of rules)."""

from pydantic import BaseModel, Field, field_validator

BR_STATES = frozenset({
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA",
    "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO",
})


class AddressBody(BaseModel):
    recipient_name: str = Field(..., min_length=2, max_length=100)
    phone: str | None = Field(None, max_length=30)
    street: str = Field(..., min_length=2, max_length=150)
    number: str = Field(..., min_length=1, max_length=20)
    complement: str | None = Field(None, max_length=100)
    neighborhood: str | None = Field(None, max_length=100)
    city: str = Field(..., min_length=2, max_length=64)
    state: str
    zip_code: str

    @field_validator("*", mode="before")
    @classmethod
    def strip(cls, v):
        return v.strip() or None if isinstance(v, str) else v

    @field_validator("state")
    @classmethod
    def valid_state(cls, v: str) -> str:
        if v.upper() not in BR_STATES:
            raise ValueError("Choose a Brazilian state")
        return v.upper()

    @field_validator("zip_code")
    @classmethod
    def format_zip(cls, v: str) -> str:
        digits = v.replace("-", "").replace(" ", "")
        if not (len(digits) == 8 and digits.isdigit()):
            raise ValueError("Enter an 8-digit CEP, like 01310-100")
        return f"{digits[:5]}-{digits[5:]}"
