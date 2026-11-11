from pydantic import BaseModel

class RegisterRequest(BaseModel):
    nickname: str
    password: str

class LoginRequest(BaseModel):
    uin: str
    password: str

class WSMessage(BaseModel):
    type: str  # message, read, typing, status
    payload: dict