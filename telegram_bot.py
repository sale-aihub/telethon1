# telegram_bot.py — Мультиаккаунт + экспорт участников группы + мгновенная работа с любыми ID
import os
import requests
from telethon.tl import functions, types
from telethon.errors import PeerIdInvalidError, UserIdInvalidError
from telethon.tl.types import InputMediaContact
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser, PeerChannel, PeerChat
from telethon.tl.functions.messages import GetDialogsRequest, GetDialogFiltersRequest
from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
from telethon.tl.types import InputPhoneContact
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PhoneNumberInvalidError, UserPrivacyRestrictedError
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator
from contextlib import asynccontextmanager
from typing import List, Optional, Union, Dict
import uvicorn
from datetime import datetime

API_ID = int(os.getenv("API_ID", 31456332))
API_HASH = os.getenv("API_HASH", "b1d1168fe0033026d00c7071a78946db")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Хранилище: имя → клиент
ACTIVE_CLIENTS = {}
PENDING_AUTH = {}

# ==================== Модели ====================
class SendMessageReq(BaseModel):
    account: str
    chat_id: str | int
    text: str

class AddAccountReq(BaseModel):
    name: str
    session_string: str

class RemoveAccountReq(BaseModel):
    name: str

class AuthStartReq(BaseModel):
    phone: str

class AuthCodeReq(BaseModel):
    phone: str
    code: str
    phone_code_hash: str
    password: str | None = None

class Auth2FAReq(BaseModel):
    phone: str
    password: str

class ExportMembersReq(BaseModel):
    account: str
    group: str | int

class DialogInfo(BaseModel):
    id: int
    title: str
    username: Optional[str] = None
    folder_names: List[str] = []
    is_group: bool
    is_channel: bool
    is_user: bool
    unread_count: int
    last_message_date: Optional[str] = None

class GetDialogsReq(BaseModel):
    account: str
    limit: int = 50
    include_folders: bool = True

class ChatMessage(BaseModel):
    id: int
    date: str
    from_id: Optional[int] = None
    text: str
    is_outgoing: bool
    
    @field_validator('from_id', mode='before')
    @classmethod
    def parse_from_id(cls, v):
        if v is None:
            return None
        if isinstance(v, (PeerUser, PeerChannel, PeerChat)):
            return v.user_id if isinstance(v, PeerUser) else v.channel_id if isinstance(v, PeerChannel) else v.chat_id
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
        return None

class GetChatHistoryReq(BaseModel):
    account: str
    chat_id: Union[str, int]
    limit: int = 50
    offset_id: Optional[int] = None

class SendToNewUserReq(BaseModel):
    account: str
    phone: str
    message: str
    first_name: str = "Contact"
    last_name: str = ""
    delete_after: bool = True

class AddContactReq(BaseModel):
    account: str
    phone: str
    first_name: str = "Contact"
    last_name: str = ""

class SendContactReq(BaseModel):
    account: str
    chat_id: Union[str, int]
    contact_id: Union[str, int]
    first_name: str = ""
    last_name: str = ""
    phone: str = ""
    message: str = ""

class GetSenderInfoReq(BaseModel):
    account: str
    chat_id: Union[str, int]
    message_id: int

class AddToChannelReq(BaseModel):
    account: str
    channel_username: str
    user_username: str
    role: str = "member"
    custom_title: Optional[str] = None

class CheckChannelMemberReq(BaseModel):
    account: str
    channel_username: str
    user_username: str

# ==================== НОВЫЕ МОДЕЛИ ДЛЯ ПОЛУЧЕНИЯ СООБЩЕНИЙ ====================
class GetLastMessageReq(BaseModel):
    account: str
    chat_id: Union[str, int]

class GetLastMessagesReq(BaseModel):
    account: str
    chat_id: Union[str, int]
    limit: int = 10
    include_media_info: bool = False

# ==================== Вспомогательные функции ====================
def extract_folder_title(folder_obj):
    if not hasattr(folder_obj, 'title'):
        return None
    
    title_obj = folder_obj.title
    if hasattr(title_obj, 'text'):
        return title_obj.text
    elif isinstance(title_obj, str):
        return title_obj
    return None

def get_chat_title(chat):
    """Получить название чата"""
    if hasattr(chat, 'title'):
        return chat.title
    elif hasattr(chat, 'first_name'):
        title = chat.first_name
        if hasattr(chat, 'last_name') and chat.last_name:
            title += f" {chat.last_name}"
        return title
    return "Unknown"

async def get_sender_info_from_message(client, message):
    """Вспомогательная функция для получения информации об отправителе сообщения"""
    try:
        sender_id = None
        
        # Определяем ID отправителя
        if hasattr(message, 'from_id') and message.from_id:
            from_id = message.from_id
            if hasattr(from_id, 'user_id'):
                sender_id = from_id.user_id
            elif hasattr(from_id, 'channel_id'):
                sender_id = from_id.channel_id
        elif hasattr(message, 'sender_id'):
            sender_id = message.sender_id
        
        if sender_id:
            try:
                sender = await client.get_entity(sender_id)
                return {
                    "id": sender.id,
                    "name": (getattr(sender, 'first_name', '') + (' ' + getattr(sender, 'last_name', '') if getattr(sender, 'last_name', '') else '')).strip(),
                    "first_name": getattr(sender, 'first_name', ''),
                    "last_name": getattr(sender, 'last_name', ''),
                    "username": getattr(sender, 'username', None),
                    "phone": getattr(sender, 'phone', None),
                    "is_bot": getattr(sender, 'bot', False)
                }
            except:
                return {"id": sender_id, "name": f"User {sender_id}"}
    except:
        pass
    return None

async def get_dialogs_with_folders_info(client: TelegramClient, limit: int = 50) -> List[DialogInfo]:
    """Получить диалоги с информацией о папках"""
    try:
        folder_info = {}
        try:
            dialog_filters_result = await client(GetDialogFiltersRequest())
            dialog_filters = getattr(dialog_filters_result, 'filters', [])
            
            for folder in dialog_filters:
                folder_title = extract_folder_title(folder)
                
                if hasattr(folder, 'id') and folder_title:
                    folder_info[folder.id] = {
                        'title': folder_title,
                        'include_peers': [],
                        'exclude_peers': []
                    }
                    
                    if hasattr(folder, 'include_peers'):
                        for peer in folder.include_peers:
                            peer_id = None
                            if hasattr(peer, 'user_id'):
                                peer_id = peer.user_id
                            elif hasattr(peer, 'chat_id'):
                                peer_id = peer.chat_id
                            elif hasattr(peer, 'channel_id'):
                                peer_id = peer.channel_id
                            
                            if peer_id:
                                folder_info[folder.id]['include_peers'].append(peer_id)
        except Exception as e:
            print(f"Ошибка получения папок: {e}")
        
        dialogs = await client.get_dialogs(limit=limit)
        dialog_to_folders = {}
        
        for folder_id, folder_data in folder_info.items():
            for peer_id in folder_data['include_peers']:
                if peer_id not in dialog_to_folders:
                    dialog_to_folders[peer_id] = []
                dialog_to_folders[peer_id].append(folder_data['title'])
        
        dialog_list = []
        for dialog in dialogs:
            entity = dialog.entity
            folder_names = []
            dialog_id = entity.id
            
            if dialog_id in dialog_to_folders:
                folder_names = dialog_to_folders[dialog_id]
            
            dialog_info = DialogInfo(
                id=entity.id,
                title=dialog.title or dialog.name or "Без названия",
                username=getattr(entity, 'username', None),
                folder_names=folder_names,
                is_group=getattr(entity, 'megagroup', False) or getattr(entity, 'gigagroup', False),
                is_channel=getattr(entity, 'broadcast', False),
                is_user=hasattr(entity, 'first_name'),
                unread_count=dialog.unread_count,
                last_message_date=dialog.date.isoformat() if dialog.date else None
            )
            dialog_list.append(dialog_info)
        
        return dialog_list
        
    except Exception as e:
        print(f"Ошибка получения диалогов: {e}")
        dialogs = await client.get_dialogs(limit=limit)
        return [DialogInfo(
            id=dialog.entity.id,
            title=dialog.title or dialog.name or "Без названия",
            username=getattr(dialog.entity, 'username', None),
            folder_names=[],
            is_group=getattr(dialog.entity, 'megagroup', False) or getattr(dialog.entity, 'gigagroup', False),
            is_channel=getattr(dialog.entity, 'broadcast', False),
            is_user=hasattr(dialog.entity, 'first_name'),
            unread_count=dialog.unread_count,
            last_message_date=dialog.date.isoformat() if dialog.date else None
        ) for dialog in dialogs]


# ==================== Lifespan ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Telegram Multi Gateway запущен на Railway")
    print(f"API_ID: {API_ID}")
    print(f"API_HASH: {API_HASH[:10]}...")
    yield
    for client in ACTIVE_CLIENTS.values():
        await client.disconnect()
    print("Все аккаунты отключены")


app = FastAPI(title="Telegram Multi Account Gateway", lifespan=lifespan)


@app.get("/")
async def root():
    return {
        "status": "running",
        "service": "Telegram Multi Account Gateway",
        "active_accounts": len(ACTIVE_CLIENTS),
        "endpoints": [
            "/auth/start", "/auth/complete", "/auth/2fa",
            "/accounts/add", "/accounts/{name}", "/accounts",
            "/send", "/dialogs", "/chat_history", "/export_members",
            "/send_to_new_user", "/add_contact", "/send_contact",
            "/get_sender_info", "/channel/add_user", "/channel/check_member",
            "/get_last_message", "/get_last_messages"
        ]
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "active_accounts": len(ACTIVE_CLIENTS)}


# ==================== Авторизация ====================
@app.post("/auth/start")
async def auth_start(req: AuthStartReq):
    """Начать авторизацию: запросить код подтверждения"""
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    try:
        sent_code = await client.send_code_request(req.phone)
        session_str = client.session.save()
        
        PENDING_AUTH[req.phone] = {
            "session_str": session_str,
            "phone_code_hash": sent_code.phone_code_hash,
            "needs_2fa": False
        }
        
        await client.disconnect()
        
        return {
            "status": "code_sent",
            "phone": req.phone,
            "phone_code_hash": sent_code.phone_code_hash,
            "needs_2fa": False
        }
    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, detail=f"Ошибка: {str(e)}")


@app.post("/auth/complete")
async def auth_complete(req: AuthCodeReq):
    """Завершить авторизацию. Автоматически определяет нужен ли 2FA."""
    pending_data = PENDING_AUTH.get(req.phone)
    if not pending_data:
        raise HTTPException(400, "Нет активной авторизации")
    
    client = TelegramClient(StringSession(pending_data["session_str"]), API_ID, API_HASH)
    await client.connect()
    
    try:
        try:
            await client.sign_in(
                phone=req.phone,
                code=req.code,
                phone_code_hash=pending_data["phone_code_hash"]
            )
            
        except SessionPasswordNeededError:
            PENDING_AUTH[req.phone]["needs_2fa"] = True
            
            if req.password:
                try:
                    await client.sign_in(password=req.password)
                except Exception as e:
                    await client.disconnect()
                    raise HTTPException(400, detail=f"Ошибка пароля 2FA: {str(e)}")
            else:
                await client.disconnect()
                return {
                    "status": "2fa_required",
                    "phone": req.phone,
                    "needs_2fa": True,
                    "message": "Требуется пароль двухфакторной аутентификации",
                    "instructions": "Используйте /auth/2fa с параметром password"
                }
        
        except Exception as e:
            await client.disconnect()
            raise HTTPException(400, detail=f"Ошибка кода: {str(e)}")
        
        session_str = client.session.save()
        del PENDING_AUTH[req.phone]
        await client.disconnect()
        
        return {
            "status": "success",
            "session_string": session_str,
            "message": "Авторизация успешна"
        }
        
    except Exception as e:
        await client.disconnect()
        raise HTTPException(500, detail=f"Неожиданная ошибка: {str(e)}")


@app.post("/auth/2fa")
async def auth_2fa(req: Auth2FAReq):
    """Отдельный эндпоинт для ввода пароля 2FA."""
    pending_data = PENDING_AUTH.get(req.phone)
    if not pending_data:
        raise HTTPException(400, "Нет активной авторизации или сессия устарела")
    
    if not pending_data.get("needs_2fa", False):
        raise HTTPException(400, "Для этого номера не требуется 2FA")
    
    client = TelegramClient(StringSession(pending_data["session_str"]), API_ID, API_HASH)
    await client.connect()
    
    try:
        await client.sign_in(password=req.password)
        
        session_str = client.session.save()
        del PENDING_AUTH[req.phone]
        await client.disconnect()
        
        return {
            "status": "success",
            "session_string": session_str,
            "message": "2FA авторизация успешна"
        }
        
    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, detail=f"Ошибка 2FA: {str(e)}")


# ==================== Работа с аккаунтами ====================
@app.post("/accounts/add")
async def add_account(req: AddAccountReq):
    if req.name in ACTIVE_CLIENTS:
        raise HTTPException(400, detail=f"Аккаунт {req.name} уже существует")

    client = TelegramClient(StringSession(req.session_string), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise HTTPException(400, detail="Сессия недействительна")

    await client.start()

    try:
        dialogs = await client.get_dialogs(limit=50)
        print(f"Прогрет кэш для {req.name}: {len(dialogs)} чатов")
    except Exception as e:
        print(f"Ошибка прогрева кэша: {e}")

    ACTIVE_CLIENTS[req.name] = client

    return {
        "status": "added",
        "account": req.name,
        "total_accounts": len(ACTIVE_CLIENTS)
    }


@app.delete("/accounts/{name}")
async def remove_account(name: str):
    client = ACTIVE_CLIENTS.pop(name, None)
    if client:
        await client.disconnect()
        return {"status": "removed", "account": name}
    raise HTTPException(404, detail="Аккаунт не найден")


@app.get("/accounts")
def list_accounts():
    return {"active_accounts": list(ACTIVE_CLIENTS.keys())}


# ==================== Отправка сообщений ====================
@app.post("/send")
async def send_message(req: SendMessageReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        await client.send_message(req.chat_id, req.text)
        return {"status": "sent", "from": req.account, "to": req.chat_id}
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка отправки: {str(e)}")


# ==================== Экспорт участников ====================
@app.post("/export_members")
async def export_members(req: ExportMembersReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        group = await client.get_entity(req.group)
        participants = await client.get_participants(group, aggressive=True)

        members = []
        for p in participants:
            is_admin = False
            admin_title = None
            
            if hasattr(p, 'participant'):
                participant = p.participant
                if hasattr(participant, 'admin_rights') and participant.admin_rights:
                    is_admin = True
                    admin_title = getattr(participant, 'rank', None) or getattr(participant, 'title', None)
            
            if not is_admin and hasattr(p, 'admin_rights') and p.admin_rights:
                is_admin = True
            
            member_data = {
                "id": p.id,
                "username": p.username if hasattr(p, 'username') and p.username else None,
                "first_name": p.first_name if hasattr(p, 'first_name') and p.first_name else "",
                "last_name": p.last_name if hasattr(p, 'last_name') and p.last_name else "",
                "phone": p.phone if hasattr(p, 'phone') and p.phone else None,
                "is_admin": is_admin,
                "admin_title": admin_title,
                "is_bot": p.bot if hasattr(p, 'bot') else False,
                "is_self": p.self if hasattr(p, 'self') else False,
                "is_contact": p.contact if hasattr(p, 'contact') else False,
                "is_mutual_contact": p.mutual_contact if hasattr(p, 'mutual_contact') else False,
                "is_deleted": p.deleted if hasattr(p, 'deleted') else False,
                "is_verified": p.verified if hasattr(p, 'verified') else False,
                "is_restricted": p.restricted if hasattr(p, 'restricted') else False,
                "is_scam": p.scam if hasattr(p, 'scam') else False,
                "is_fake": p.fake if hasattr(p, 'fake') else False,
                "is_support": p.support if hasattr(p, 'support') else False,
                "is_premium": p.premium if hasattr(p, 'premium') else False,
            }
            
            if hasattr(p, 'status'):
                status = p.status
                if hasattr(status, '__class__'):
                    member_data["status"] = status.__class__.__name__
                    if hasattr(status, 'was_online'):
                        member_data["last_seen"] = status.was_online.isoformat() if status.was_online else None
            
            members.append(member_data)

        return {
            "status": "exported",
            "group": req.group,
            "group_title": group.title if hasattr(group, 'title') else "Unknown",
            "total_members": len(members),
            "admins_count": sum(1 for m in members if m["is_admin"]),
            "bots_count": sum(1 for m in members if m["is_bot"]),
            "members": members
        }
    except Exception as e:
        print(f"Ошибка экспорта участников: {e}")
        raise HTTPException(500, detail=f"Ошибка экспорта: {str(e)}")


# ==================== Диалоги ====================
@app.post("/dialogs")
async def get_dialogs(req: GetDialogsReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        if req.include_folders:
            dialog_list = await get_dialogs_with_folders_info(client, req.limit)
        else:
            dialogs = await client.get_dialogs(limit=req.limit)
            dialog_list = [
                DialogInfo(
                    id=dialog.entity.id,
                    title=dialog.title or dialog.name or "Без названия",
                    username=getattr(dialog.entity, 'username', None),
                    folder_names=[],
                    is_group=getattr(dialog.entity, 'megagroup', False) or getattr(dialog.entity, 'gigagroup', False),
                    is_channel=getattr(dialog.entity, 'broadcast', False),
                    is_user=hasattr(dialog.entity, 'first_name'),
                    unread_count=dialog.unread_count,
                    last_message_date=dialog.date.isoformat() if dialog.date else None
                ) for dialog in dialogs
            ]
        
        return {
            "status": "success",
            "account": req.account,
            "total_dialogs": len(dialog_list),
            "dialogs": dialog_list
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения диалогов: {str(e)}")


@app.post("/folders/{account}")
async def get_all_folders(account: str):
    client = ACTIVE_CLIENTS.get(account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {account}")

    try:
        dialog_filters_result = await client(GetDialogFiltersRequest())
        dialog_filters = getattr(dialog_filters_result, 'filters', [])
        folders = []
        
        for folder in dialog_filters:
            folder_title = extract_folder_title(folder)
            
            if hasattr(folder, 'id') and folder_title:
                folder_info = {
                    "id": folder.id,
                    "title": folder_title,
                    "color": getattr(folder, 'color', None),
                    "pinned": getattr(folder, 'pinned', False),
                    "include_count": len(getattr(folder, 'include_peers', [])),
                    "exclude_count": len(getattr(folder, 'exclude_peers', []))
                }
                folders.append(folder_info)
        
        return {
            "status": "success",
            "account": account,
            "total_folders": len(folders),
            "folders": folders
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения папок: {str(e)}")


# ==================== История чата ====================
@app.post("/chat_history")
async def get_chat_history(req: GetChatHistoryReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        chat_id = req.chat_id
        
        if isinstance(chat_id, str):
            if chat_id.startswith('@'):
                chat_id = chat_id[1:]
            if chat_id.lstrip('-').isdigit():
                chat_id = int(chat_id)
        
        try:
            chat = await client.get_entity(chat_id)
        except Exception:
            dialogs = await client.get_dialogs()
            for dialog in dialogs:
                if str(dialog.id) == str(chat_id) or (hasattr(dialog.entity, 'username') and dialog.entity.username == chat_id):
                    chat = dialog.entity
                    break
            else:
                raise HTTPException(400, detail=f"Не удалось найти чат: {req.chat_id}")
        
        messages = await client.get_messages(
            chat,
            limit=req.limit,
            offset_id=req.offset_id if req.offset_id and req.offset_id > 0 else None
        )
        
        message_list = []
        for msg in messages:
            if msg is None:
                continue
                
            text = ""
            if hasattr(msg, 'text') and msg.text:
                text = msg.text
            elif hasattr(msg, 'message') and msg.message:
                text = msg.message
            
            if not text and not hasattr(msg, 'media'):
                continue
            
            message = ChatMessage(
                id=msg.id,
                date=msg.date.isoformat() if msg.date else "",
                from_id=None,
                text=text,
                is_outgoing=msg.out if hasattr(msg, 'out') else False
            )
            message_list.append(message)
        
        chat_title = "Unknown"
        if hasattr(chat, 'title'):
            chat_title = chat.title
        elif hasattr(chat, 'first_name'):
            chat_title = chat.first_name
            if hasattr(chat, 'last_name') and chat.last_name:
                chat_title += f" {chat.last_name}"
        
        return {
            "status": "success",
            "account": req.account,
            "chat_id": req.chat_id,
            "chat_title": chat_title,
            "total_messages": len(message_list),
            "messages": message_list
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения истории: {str(e)}")


# ==================== НОВЫЙ ЭНДПОИНТ: Получить последнее сообщение ====================
@app.post("/get_last_message")
async def get_last_message(req: GetLastMessageReq):
    """
    Получить последнее сообщение в указанном чате.
    
    Параметры:
    - account: имя аккаунта
    - chat_id: ID чата, username или @username
    
    Возвращает:
    - Информацию о последнем сообщении: текст, отправитель, дата, ID и т.д.
    """
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        # 1. Получаем сущность чата
        chat_id = req.chat_id
        
        if isinstance(chat_id, str):
            if chat_id.startswith('@'):
                chat_id = chat_id[1:]
            if chat_id.lstrip('-').isdigit():
                chat_id = int(chat_id)
        
        try:
            chat = await client.get_entity(chat_id)
        except Exception:
            dialogs = await client.get_dialogs()
            for dialog in dialogs:
                if str(dialog.id) == str(chat_id) or (hasattr(dialog.entity, 'username') and dialog.entity.username == chat_id):
                    chat = dialog.entity
                    break
            else:
                raise HTTPException(400, detail=f"Не удалось найти чат: {req.chat_id}")
        
        # 2. Получаем последнее сообщение
        messages = await client.get_messages(chat, limit=1)
        
        if not messages or len(messages) == 0:
            return {
                "status": "success",
                "account": req.account,
                "chat_id": req.chat_id,
                "chat_title": get_chat_title(chat),
                "has_messages": False,
                "message": None,
                "note": "В этом чате нет сообщений"
            }
        
        message = messages[0]
        
        # 3. Получаем информацию об отправителе
        sender_info = await get_sender_info_from_message(client, message)
        
        # 4. Определяем тип сообщения
        message_type = "text"
        media_info = None
        
        if message.media:
            if hasattr(message.media, 'document'):
                message_type = "document"
                media_info = {
                    "name": "Unknown",
                    "size": getattr(message.media.document, 'size', 0),
                    "mime_type": getattr(message.media.document, 'mime_type', '')
                }
                if hasattr(message.media.document, 'attributes'):
                    for attr in message.media.document.attributes:
                        if hasattr(attr, 'file_name'):
                            media_info["name"] = attr.file_name
                            break
            elif hasattr(message.media, 'photo'):
                message_type = "photo"
            elif hasattr(message.media, 'video'):
                message_type = "video"
            elif hasattr(message.media, 'audio'):
                message_type = "audio"
            elif hasattr(message.media, 'voice'):
                message_type = "voice"
            elif hasattr(message.media, 'contact'):
                message_type = "contact"
                media_info = {
                    "first_name": message.media.first_name,
                    "last_name": message.media.last_name,
                    "phone_number": message.media.phone_number
                }
            elif hasattr(message.media, 'geo'):
                message_type = "location"
            else:
                message_type = "media"
        
        # 5. Формируем ответ
        response = {
            "status": "success",
            "account": req.account,
            "chat_id": req.chat_id,
            "chat_title": get_chat_title(chat),
            "has_messages": True,
            "last_message": {
                "id": message.id,
                "date": message.date.isoformat() if message.date else None,
                "text": message.text or message.message or "",
                "is_outgoing": message.out if hasattr(message, 'out') else False,
                "message_type": message_type,
                "has_media": bool(message.media),
                "has_reply": bool(message.reply_to) if hasattr(message, 'reply_to') else False,
                "is_forward": bool(message.forward) if hasattr(message, 'forward') else False,
                "views": message.views if hasattr(message, 'views') else None,
                "forwards": message.forwards if hasattr(message, 'forwards') else None,
                "sender": sender_info,
                "media_info": media_info
            },
            "timestamp": datetime.now().isoformat()
        }
        
        return response
        
    except PeerIdInvalidError:
        raise HTTPException(400, detail="Неверный ID чата или пользователя")
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Ошибка получения последнего сообщения: {error_msg}")
        
        if "CHANNEL_PRIVATE" in error_msg:
            raise HTTPException(403, detail="Нет доступа к указанному каналу")
        elif "CHAT_FORBIDDEN" in error_msg:
            raise HTTPException(403, detail="Нет доступа к указанному чату")
        else:
            raise HTTPException(500, detail=f"Ошибка получения последнего сообщения: {error_msg}")


# ==================== НОВЫЙ ЭНДПОИНТ: Получить последние N сообщений ====================
@app.post("/get_last_messages")
async def get_last_messages(req: GetLastMessagesReq):
    """
    Получить последние N сообщений в чате.
    
    Параметры:
    - account: имя аккаунта
    - chat_id: ID чата, username или @username
    - limit: количество сообщений (1-100, по умолчанию 10)
    - include_media_info: включать ли детальную информацию о медиа (по умолчанию False)
    """
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    limit = min(max(req.limit, 1), 100)
    
    try:
        chat_id = req.chat_id
        
        if isinstance(chat_id, str):
            if chat_id.startswith('@'):
                chat_id = chat_id[1:]
            if chat_id.lstrip('-').isdigit():
                chat_id = int(chat_id)
        
        try:
            chat = await client.get_entity(chat_id)
        except Exception:
            dialogs = await client.get_dialogs()
            for dialog in dialogs:
                if str(dialog.id) == str(chat_id) or (hasattr(dialog.entity, 'username') and dialog.entity.username == chat_id):
                    chat = dialog.entity
                    break
            else:
                raise HTTPException(400, detail=f"Не удалось найти чат: {req.chat_id}")
        
        messages = await client.get_messages(chat, limit=limit)
        
        if not messages or len(messages) == 0:
            return {
                "status": "success",
                "account": req.account,
                "chat_id": req.chat_id,
                "chat_title": get_chat_title(chat),
                "has_messages": False,
                "total_messages": 0,
                "messages": []
            }
        
        messages_list = []
        for message in messages:
            if message is None:
                continue
            
            message_type = "text"
            if message.media:
                if hasattr(message.media, 'document'):
                    message_type = "document"
                elif hasattr(message.media, 'photo'):
                    message_type = "photo"
                elif hasattr(message.media, 'video'):
                    message_type = "video"
                elif hasattr(message.media, 'audio'):
                    message_type = "audio"
                elif hasattr(message.media, 'contact'):
                    message_type = "contact"
                elif hasattr(message.media, 'geo'):
                    message_type = "location"
                else:
                    message_type = "media"
            
            msg_data = {
                "id": message.id,
                "date": message.date.isoformat() if message.date else None,
                "text": (message.text or message.message or "")[:500],
                "is_outgoing": message.out if hasattr(message, 'out') else False,
                "message_type": message_type,
                "has_media": bool(message.media)
            }
            
            sender_info = await get_sender_info_from_message(client, message)
            if sender_info:
                msg_data["sender"] = sender_info
            
            if req.include_media_info and message.media:
                if hasattr(message.media, 'contact'):
                    msg_data["contact_info"] = {
                        "first_name": message.media.first_name,
                        "last_name": message.media.last_name,
                        "phone_number": message.media.phone_number
                    }
                elif hasattr(message.media, 'document'):
                    msg_data["document_info"] = {
                        "size": getattr(message.media.document, 'size', 0),
                        "mime_type": getattr(message.media.document, 'mime_type', '')
                    }
            
            messages_list.append(msg_data)
        
        return {
            "status": "success",
            "account": req.account,
            "chat_id": req.chat_id,
            "chat_title": get_chat_title(chat),
            "has_messages": True,
            "total_messages": len(messages_list),
            "requested_limit": limit,
            "messages": messages_list,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Ошибка получения последних сообщений: {error_msg}")
        raise HTTPException(500, detail=f"Ошибка получения сообщений: {error_msg}")


# ==================== Дополнительные эндпоинты ====================
@app.post("/send_to_new_user")
async def send_to_new_user(req: SendToNewUserReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        contact = InputPhoneContact(
            client_id=0,
            phone=req.phone,
            first_name=req.first_name,
            last_name=req.last_name
        )
        
        result = await client(ImportContactsRequest([contact]))
        
        if not result.users:
            raise HTTPException(400, detail=f"Пользователь не найден по номеру {req.phone}")
        
        user = result.users[0]
        
        try:
            await client.send_message(user, req.message)
            
            if req.delete_after:
                await client(DeleteContactsRequest(id=[user]))
            
            return {
                "status": "sent",
                "account": req.account,
                "phone": req.phone,
                "user_id": user.id,
                "user_info": {
                    "first_name": user.first_name,
                    "last_name": user.last_name or "",
                    "username": getattr(user, 'username', None)
                },
                "deleted_from_contacts": req.delete_after
            }
            
        except FloodWaitError as e:
            if not req.delete_after:
                try:
                    await client(DeleteContactsRequest(id=[user]))
                except:
                    pass
            raise HTTPException(429, detail=f"Ограничение Telegram: ждите {e.seconds} секунд")
            
    except PhoneNumberInvalidError:
        raise HTTPException(400, detail=f"Некорректный номер телефона: {req.phone}")
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка: {str(e)}")


@app.post("/add_contact")
async def add_contact(req: AddContactReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        contact = InputPhoneContact(
            client_id=0,
            phone=req.phone,
            first_name=req.first_name,
            last_name=req.last_name
        )
        
        result = await client(ImportContactsRequest([contact]))
        
        if not result.users:
            raise HTTPException(400, detail=f"Пользователь не найден по номеру {req.phone}")
        
        user = result.users[0]
        
        return {
            "status": "contact_added",
            "account": req.account,
            "phone": req.phone,
            "contact": {
                "id": user.id,
                "first_name": user.first_name,
                "last_name": user.last_name or "",
                "username": getattr(user, 'username', None)
            }
        }
        
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка: {str(e)}")


@app.post("/send_contact")
async def send_contact(req: SendContactReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        if not req.phone:
            raise HTTPException(400, detail="Параметр 'phone' обязателен")
        if not req.first_name:
            raise HTTPException(400, detail="Параметр 'first_name' обязателен")
        
        chat_entity = await client.get_entity(req.chat_id)
        
        media_contact = types.InputMediaContact(
            phone_number=req.phone,
            first_name=req.first_name,
            last_name=req.last_name,
            vcard=''
        )
        
        result = await client.send_message(
            entity=chat_entity,
            message=req.message if req.message else "",
            file=media_contact
        )
        
        return {
            "status": "success",
            "account": req.account,
            "chat_id": req.chat_id,
            "contact": {
                "phone": req.phone,
                "first_name": req.first_name,
                "last_name": req.last_name
            },
            "message": {
                "id": result.id,
                "text": req.message
            }
        }
        
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка отправки контакта: {str(e)}")


@app.post("/get_sender_info")
async def get_sender_info(req: GetSenderInfoReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        chat = await client.get_entity(req.chat_id)
        messages = await client.get_messages(entity=chat, ids=req.message_id)
        
        if not messages or (isinstance(messages, list) and len(messages) == 0):
            raise HTTPException(404, detail=f"Сообщение с ID {req.message_id} не найдено")
        
        message = messages[0] if isinstance(messages, list) else messages
        
        sender_id = None
        if hasattr(message, 'from_id') and message.from_id:
            from_id = message.from_id
            if hasattr(from_id, 'user_id'):
                sender_id = from_id.user_id
            elif hasattr(from_id, 'channel_id'):
                sender_id = from_id.channel_id
        
        if not sender_id and hasattr(message, 'sender_id'):
            sender_id = message.sender_id
        
        if not sender_id:
            raise HTTPException(404, detail="Не удалось определить отправителя")
        
        sender = await client.get_entity(sender_id)
        
        return {
            "status": "success",
            "account": req.account,
            "sender": {
                "id": sender.id,
                "first_name": getattr(sender, 'first_name', ''),
                "last_name": getattr(sender, 'last_name', ''),
                "username": getattr(sender, 'username', None),
                "phone": getattr(sender, 'phone', None),
                "is_bot": getattr(sender, 'bot', False)
            },
            "message": {
                "id": message.id,
                "date": message.date.isoformat() if message.date else None,
                "text": message.text or message.message or ""
            }
        }
        
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка: {str(e)}")


@app.post("/channel/add_user")
async def add_user_to_channel(req: AddToChannelReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        channel = await client.get_entity(req.channel_username)
        user = await client.get_entity(req.user_username)
        
        if req.role in ["admin", "moderator"]:
            admin_rights = types.ChatAdminRights(
                change_info=True, post_messages=True, edit_messages=True,
                delete_messages=True, ban_users=True, invite_users=True,
                pin_messages=True, add_admins=True
            )
            result = await client(functions.channels.EditAdminRequest(
                channel=channel,
                user_id=user.id,
                admin_rights=admin_rights,
                rank=req.custom_title or "Администратор"
            ))
        else:
            result = await client(functions.channels.InviteToChannelRequest(
                channel=channel,
                users=[user]
            ))
        
        return {
            "status": "success",
            "account": req.account,
            "channel": channel.title,
            "user": user.first_name,
            "role": req.role
        }
        
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка: {str(e)}")


@app.post("/channel/check_member")
async def check_channel_member(req: CheckChannelMemberReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        channel = await client.get_entity(req.channel_username)
        user = await client.get_entity(req.user_username)
        
        participants = await client.get_participants(channel)
        is_member = any(p.id == user.id for p in participants)
        
        return {
            "status": "success",
            "is_member": is_member,
            "channel": channel.title,
            "user": user.first_name
        }
        
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка: {str(e)}")


# ==================== Запуск ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("telegram_bot:app", host="0.0.0.0", port=port, reload=False)
