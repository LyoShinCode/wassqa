import jwt
import bcrypt
import os
from datetime import datetime, timedelta

# Длинный секретный ключ (64 символа)
SECRET_KEY = os.getenv("JWT_SECRET", "wasqa-super-secret-jwt-key-for-development-only-2026")

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), hash.encode())

def create_token(user_id: int, uin: str) -> str:
    payload = {
        "user_id": user_id,
        "uin": uin,
        "exp": datetime.utcnow() + timedelta(days=7)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except:
        return None