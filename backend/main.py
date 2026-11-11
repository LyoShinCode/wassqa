import random

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from auth import hash_password, verify_password, create_token, decode_token
from db import init_db, get_db
from models import RegisterRequest, LoginRequest

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory состояние
active_connections: dict[int, WebSocket] = {}
user_statuses: dict[int, str] = {}
typing_users: dict[int, set] = {}  # chat_id -> {user_ids}


@app.on_event("startup")
async def startup():
    init_db()


# ---------- REST API ----------

@app.post("/api/register")
async def register(req: RegisterRequest):
    with get_db() as conn:
        cur = conn.cursor()

        for attempt in range(100):
            uin = str(random.randint(100000000, 999999999))
            existing = cur.execute("SELECT id FROM users WHERE uin=?", (uin,)).fetchone()
            if not existing:
                break
        else:
            raise HTTPException(500, "Не удалось сгенерировать UIN")

        cur.execute(
            "INSERT INTO users (uin, nickname, password_hash) VALUES (?,?,?)",
            (uin, req.nickname, hash_password(req.password))
        )
        user_id = cur.lastrowid
        token = create_token(user_id, uin)
        return {
            "token": token,
            "user_id": user_id,
            "uin": uin,
            "nickname": req.nickname
        }


@app.post("/api/login")
async def login(req: LoginRequest):
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM users WHERE uin=?", (req.uin,)).fetchone()
        if not row or not verify_password(req.password, row["password_hash"]):
            raise HTTPException(401, "Неверный логин или пароль")
        token = create_token(row["id"], row["uin"])
        return {"token": token, "user_id": row["id"], "uin": row["uin"], "nickname": row["nickname"]}


@app.get("/api/contacts")
async def get_contacts(token: str):
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401)
    user_id = payload["user_id"]
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT u.id, u.uin, u.nickname, c.status
            FROM contacts c
            JOIN users u ON u.id = c.contact_id
            WHERE c.user_id = ? AND c.status = 'accepted'
        """, (user_id,)).fetchall()
        contacts = []
        for r in rows:
            contacts.append({
                **dict(r),
                "online_status": user_statuses.get(r["id"], "offline")
            })
        return contacts


@app.post("/api/contacts/add")
async def add_contact(uin: str, token: str):
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401)
    user_id = payload["user_id"]
    with get_db() as conn:
        cur = conn.cursor()
        target = cur.execute("SELECT id FROM users WHERE uin=?", (uin,)).fetchone()
        if not target:
            raise HTTPException(404, "Пользователь не найден")
        if target["id"] == user_id:
            raise HTTPException(400, "Нельзя добавить себя")
        # Автодобавление (упрощение для MVP)
        cur.execute("INSERT OR IGNORE INTO contacts (user_id, contact_id, status) VALUES (?,?,?)",
                    (user_id, target["id"], "accepted"))
        cur.execute("INSERT OR IGNORE INTO contacts (user_id, contact_id, status) VALUES (?,?,?)",
                    (target["id"], user_id, "accepted"))
        # Создаём direct-чат, если его нет
        existing_chat = cur.execute("""
            SELECT id FROM chats WHERE type='direct' AND (
                (user1_id=? AND user2_id=?) OR (user1_id=? AND user2_id=?)
            )
        """, (user_id, target["id"], target["id"], user_id)).fetchone()
        if not existing_chat:
            cur.execute("INSERT INTO chats (type, user1_id, user2_id) VALUES ('direct', ?, ?)",
                        (user_id, target["id"]))
        return {"ok": True}


@app.delete("/api/contacts/{contact_id}")
async def remove_contact(contact_id: int, token: str):
    """Удалить контакт (в обе стороны)"""
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401, "Невалидный токен")

    user_id = payload["user_id"]

    with get_db() as conn:
        cur = conn.cursor()
        # Удаляем связь в обе стороны
        cur.execute(
            "DELETE FROM contacts WHERE user_id=? AND contact_id=?",
            (user_id, contact_id)
        )
        cur.execute(
            "DELETE FROM contacts WHERE user_id=? AND contact_id=?",
            (contact_id, user_id)
        )

    return {"ok": True}


@app.get("/api/chats/direct/{contact_id}")
async def get_or_create_direct_chat(contact_id: int, token: str):
    """Получить или создать direct-чат с контактом"""
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401, "Невалидный токен")

    user_id = payload["user_id"]

    if user_id == contact_id:
        raise HTTPException(400, "Нельзя создать чат с самим собой")

    with get_db() as conn:
        cur = conn.cursor()

        # Проверяем что contact_id существует
        contact_exists = cur.execute(
            "SELECT id FROM users WHERE id=?", (contact_id,)
        ).fetchone()

        if not contact_exists:
            raise HTTPException(404, f"Пользователь с id={contact_id} не найден")

        # Ищем существующий чат
        existing = cur.execute("""
            SELECT id FROM chats 
            WHERE type='direct' AND (
                (user1_id=? AND user2_id=?) OR 
                (user1_id=? AND user2_id=?)
            )
        """, (user_id, contact_id, contact_id, user_id)).fetchone()

        if existing:
            return {"chat_id": existing["id"]}

        # Создаём новый чат
        cur.execute(
            "INSERT INTO chats (type, user1_id, user2_id) VALUES ('direct', ?, ?)",
            (user_id, contact_id)
        )
        chat_id = cur.lastrowid
        return {"chat_id": chat_id}


@app.get("/api/messages/{chat_id}")
async def get_messages(chat_id: int, token: str):
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401)
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT id, author_id, text, created_at
            FROM messages WHERE chat_id=? AND is_deleted=0
            ORDER BY id DESC LIMIT 50
        """, (chat_id,)).fetchall()
        return [dict(r) for r in reversed(rows)]


@app.post("/api/messages/{chat_id}/read")
async def mark_read(chat_id: int, msg_id: int, token: str):
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401)
    user_id = payload["user_id"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO read_receipts (user_id, chat_id, last_read_msg_id)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, chat_id) DO UPDATE SET last_read_msg_id=excluded.last_read_msg_id
        """, (user_id, chat_id, msg_id))
    return {"ok": True}


@app.delete("/api/messages/{msg_id}")
async def delete_message(msg_id: int, token: str):
    """Soft-delete сообщения (пометка is_deleted=1)"""
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401)
    user_id = payload["user_id"]

    with get_db() as conn:
        cur = conn.cursor()
        # Проверяем что сообщение принадлежит пользователю
        msg = cur.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        if not msg:
            raise HTTPException(404, "Сообщение не найдено")
        if msg["author_id"] != user_id:
            raise HTTPException(403, "Можно удалять только свои сообщения")

        cur.execute("UPDATE messages SET is_deleted=1 WHERE id=?", (msg_id,))

    # Рассылаем участникам чата
    await broadcast(msg["chat_id"], {
        "type": "message:deleted",
        "payload": {"msg_id": msg_id, "chat_id": msg["chat_id"]}
    })

    return {"ok": True}


@app.patch("/api/messages/{msg_id}")
async def edit_message(msg_id: int, text: str, token: str):
    """Редактирование сообщения"""
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401)
    user_id = payload["user_id"]

    with get_db() as conn:
        cur = conn.cursor()
        msg = cur.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        if not msg:
            raise HTTPException(404)
        if msg["author_id"] != user_id:
            raise HTTPException(403, "Можно редактировать только свои сообщения")
        if msg["is_deleted"]:
            raise HTTPException(400, "Нельзя редактировать удалённое сообщение")

        cur.execute("UPDATE messages SET text=? WHERE id=?", (text, msg_id))

    # Рассылаем обновление
    await broadcast(msg["chat_id"], {
        "type": "message:edited",
        "payload": {"msg_id": msg_id, "chat_id": msg["chat_id"], "text": text}
    })

    return {"ok": True}


@app.get("/api/contacts")
async def get_contacts(token: str):
    """Список контактов + счётчик непрочитанных"""
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401)
    user_id = payload["user_id"]
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT u.id, u.uin, u.nickname, c.status
            FROM contacts c
            JOIN users u ON u.id = c.contact_id
            WHERE c.user_id = ? AND c.status = 'accepted'
        """, (user_id,)).fetchall()

        contacts = []
        for r in rows:
            # Находим chat_id для этого контакта
            chat = cur.execute("""
                SELECT id FROM chats 
                WHERE type='direct' AND (
                    (user1_id=? AND user2_id=?) OR (user1_id=? AND user2_id=?)
                )
            """, (user_id, r["id"], r["id"], user_id)).fetchone()

            unread = 0
            if chat:
                # Считаем непрочитанные
                last_read = cur.execute("""
                    SELECT last_read_msg_id FROM read_receipts 
                    WHERE user_id=? AND chat_id=?
                """, (user_id, chat["id"])).fetchone()
                last_id = last_read["last_read_msg_id"] if last_read else 0

                unread_row = cur.execute("""
                    SELECT COUNT(*) as cnt FROM messages 
                    WHERE chat_id=? AND id>? AND is_deleted=0
                """, (chat["id"], last_id)).fetchone()
                unread = unread_row["cnt"]

            contacts.append({
                **dict(r),
                "online_status": user_statuses.get(r["id"], "offline"),
                "unread": unread
            })
        return contacts


# ---------- WebSocket ----------

async def broadcast(chat_id: int, message: dict, exclude_user_id: int = None):
    """Отправить сообщение всем участникам чата"""
    with get_db() as conn:
        cur = conn.cursor()
        chat = cur.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not chat:
            return
        participants = []
        if chat["type"] == "direct":
            participants = [chat["user1_id"], chat["user2_id"]]
        # Для групповых — отдельная таблица members (MVP не делаем)

    for uid in participants:
        if uid == exclude_user_id:
            continue
        ws = active_connections.get(uid)
        if ws:
            try:
                await ws.send_json(message)
            except:
                pass


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str):
    payload = decode_token(token)
    if not payload:
        await websocket.close(code=1008)
        return

    user_id = payload["user_id"]
    await websocket.accept()
    active_connections[user_id] = websocket
    user_statuses[user_id] = "online"

    # Рассылаем всем контактам, что пользователь онлайн
    await broadcast_status_change(user_id, "online")

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "message":
                chat_id = data["payload"]["chat_id"]
                text = data["payload"]["text"]

                from datetime import datetime
                created_at = datetime.now().isoformat()

                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO messages (chat_id, author_id, text, created_at) VALUES (?,?,?,?)",
                        (chat_id, user_id, text, created_at)
                    )
                    msg_id = cur.lastrowid

                # Подтверждение отправителю
                await websocket.send_json({
                    "type": "message:ack",
                    "payload": {"chat_id": chat_id, "msg_id": msg_id, "status": "delivered"}
                })
                # Отправка собеседнику
                await broadcast(chat_id, {
                    "type": "message:new",
                    "payload": {
                        "msg_id": msg_id,
                        "chat_id": chat_id,
                        "author_id": user_id,
                        "text": text,
                        "created_at": created_at
                    }
                }, exclude_user_id=user_id)

            elif msg_type == "read":
                chat_id = data["payload"]["chat_id"]
                msg_id = data["payload"]["msg_id"]
                # Сохраняем в БД
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO read_receipts (user_id, chat_id, last_read_msg_id)
                        VALUES (?,?,?)
                        ON CONFLICT(user_id, chat_id) DO UPDATE SET last_read_msg_id=excluded.last_read_msg_id
                    """, (user_id, chat_id, msg_id))
                # Рассылаем собеседнику
                await broadcast(chat_id, {
                    "type": "message:read",
                    "payload": {"chat_id": chat_id, "user_id": user_id, "msg_id": msg_id}
                }, exclude_user_id=user_id)

            elif msg_type == "typing":
                chat_id = data["payload"]["chat_id"]
                await broadcast(chat_id, {
                    "type": "typing",
                    "payload": {"chat_id": chat_id, "user_id": user_id, "is_typing": True}
                }, exclude_user_id=user_id)

            elif msg_type == "status":
                new_status = data["payload"]["status"]
                user_statuses[user_id] = new_status
                with get_db() as conn:
                    conn.execute("UPDATE users SET status=? WHERE id=?", (new_status, user_id))
                await broadcast_status_change(user_id, new_status)

    except WebSocketDisconnect:
        active_connections.pop(user_id, None)
        user_statuses[user_id] = "offline"
        await broadcast_status_change(user_id, "offline")


async def broadcast_status_change(user_id: int, status: str):
    """Отправить статус всем контактам пользователя"""
    with get_db() as conn:
        cur = conn.cursor()
        contacts = cur.execute("""
            SELECT contact_id FROM contacts
            WHERE user_id=? AND status='accepted'
        """, (user_id,)).fetchall()

    for c in contacts:
        ws = active_connections.get(c["contact_id"])
        if ws:
            try:
                await ws.send_json({
                    "type": "presence:update",
                    "payload": {"user_id": user_id, "status": status}
                })
            except:
                pass


# ---------- Статика ----------
app.mount("/static", StaticFiles(directory="../frontend"), name="static")


@app.get("/")
async def root():
    return FileResponse("../frontend/index.html")
