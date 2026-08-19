import os
import re
import time
import json
import sqlite3
import asyncio
import warnings
import csv
import hashlib
import datetime
import shutil
from telethon import TelegramClient, events, errors
from telethon.tl.types import User, Channel, Chat, ChannelForbidden, MessageMediaWebPage
from jinja2 import Environment, FileSystemLoader
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from telethon.tl.functions.contacts import GetContactsRequest

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

ENTITY_BACKUP_DIR_NAMES = {}

def load_env_file(path=".env"):
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

def get_required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value

def get_telegram_config():
    load_env_file()
    try:
        api_id = int(get_required_env("TELEGRAM_API_ID"))
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_API_ID должен быть целым числом") from exc

    api_hash = get_required_env("TELEGRAM_API_HASH")
    phone_number = os.getenv("TELEGRAM_PHONE_NUMBER")
    return api_id, api_hash, phone_number

def get_url_from_forwarded(forwarded):
    if forwarded is None:
        return None
    match = re.search(r"channel_id=(\d+).*channel_post=(\d+)", forwarded)
    if match:
        channel_id, channel_post = match.groups()
        return f"https://t.me/c/{channel_id}/{channel_post}"
    return None

def sanitize_filename(filename):
    return re.sub(r'[^\w\-_\. ]', '_', filename)

def is_yes(value):
    return value.strip().lower() in {"y", "yes"}

def get_entity_username(entity):
    username = getattr(entity, "username", None)
    if username:
        return username.lstrip("@")
    return None

def get_entity_profile_dir_name(entity_id, entity=None):
    username = get_entity_username(entity)
    if username:
        return sanitize_filename(f"{entity_id}_{username}")
    return str(entity_id)

def register_entity_backup_dir(entity_id, entity):
    ENTITY_BACKUP_DIR_NAMES[int(entity_id)] = get_entity_profile_dir_name(entity_id, entity)

def get_entity_backup_dir(entity_id):
    profile_dir_name = ENTITY_BACKUP_DIR_NAMES.get(int(entity_id), str(entity_id))
    return os.path.join("data", profile_dir_name)

def get_legacy_entity_backup_dir(entity_id):
    return os.path.join("data", str(entity_id))

def get_entity_media_dir(entity_id):
    return os.path.join(get_entity_backup_dir(entity_id), "media")

def get_entity_html_path(entity_id, chat_name):
    return os.path.join(get_entity_backup_dir(entity_id), f"{chat_name}.html")

def normalize_media_path_for_html(media_file, entity_id):
    if not media_file:
        return media_file

    media_path = resolve_media_path(media_file, entity_id)
    if media_path:
        return os.path.relpath(media_path, get_entity_backup_dir(entity_id))

    return media_file

def get_media_db_path(media_file, entity_id):
    return os.path.relpath(media_file, get_entity_backup_dir(entity_id))

def resolve_media_path(media_file, entity_id):
    if not media_file:
        return None

    candidates = []
    if os.path.isabs(media_file):
        candidates.append(media_file)
    else:
        legacy_backup_dir = get_legacy_entity_backup_dir(entity_id)
        candidates.append(media_file)
        candidates.append(os.path.join(get_entity_backup_dir(entity_id), media_file))
        candidates.append(os.path.join(get_entity_media_dir(entity_id), os.path.basename(media_file)))
        candidates.append(os.path.join(legacy_backup_dir, media_file))
        candidates.append(os.path.join(legacy_backup_dir, "media", os.path.basename(media_file)))
        candidates.append(os.path.join("media", str(entity_id), os.path.basename(media_file)))
        candidates.append(os.path.join(str(entity_id), os.path.basename(media_file)))

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    return None

def normalize_message_media_path(message, entity_id):
    if not entity_id or not message[4]:
        return message

    normalized = list(message)
    normalized[4] = normalize_media_path_for_html(message[4], entity_id)
    return tuple(normalized)

def prepare_entity_db(entity_id, sanitized_name):
    backup_dir = get_entity_backup_dir(entity_id)
    legacy_backup_dir = get_legacy_entity_backup_dir(entity_id)
    os.makedirs(backup_dir, exist_ok=True)

    db_name = f"{sanitized_name}.db"
    db_path = os.path.join(backup_dir, db_name)
    legacy_db_path = os.path.join(legacy_backup_dir, db_name)
    if not os.path.exists(db_path) and os.path.exists(db_name):
        shutil.copy2(db_name, db_path)
        print(f"Существующая база скопирована в {db_path}")
    elif not os.path.exists(db_path) and backup_dir != legacy_backup_dir and os.path.exists(legacy_db_path):
        shutil.copy2(legacy_db_path, db_path)
        print(f"Существующая база скопирована в {db_path}")

    return db_path

def get_file_hash(file_path):
    if not os.path.exists(file_path):
        return None
    
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def get_message_media_id(message):
    if not message.media:
        return "media"

    if hasattr(message.media, "document") and message.media.document:
        return str(message.media.document.id)

    if hasattr(message.media, "photo") and message.media.photo:
        return str(message.media.photo.id)

    return message.media.__class__.__name__

def get_media_target_path(message, entity_id, media_type):
    media_dir = get_entity_media_dir(entity_id)
    media_id = get_message_media_id(message)
    extension = ""

    if getattr(message, "file", None) and message.file.ext:
        extension = message.file.ext

    filename = f"{message.id}_{media_id}{extension}"

    return os.path.join(media_dir, filename)

def is_downloadable_media(message):
    if not message.media:
        return False

    return bool(
        getattr(message, "file", None)
        or getattr(message, "photo", None)
        or (
            hasattr(message.media, "document")
            and message.media.document
        )
        or (
            hasattr(message.media, "photo")
            and message.media.photo
        )
    )

def extract_user_id(from_id_str):
    if not from_id_str:
        return None
    
    match = re.search(r"user_id=(\d+)", from_id_str)
    if match:
        return match.group(1)
    
    match = re.search(r"channel_id=(\d+)", from_id_str)
    if match:
        return match.group(1)
    
    match = re.search(r"chat_id=(\d+)", from_id_str)
    if match:
        return match.group(1)
    
    if from_id_str.isdigit():
        return from_id_str
    
    return None

async def get_contacts(client, phone_number):
    print("Извлекаю список контактов...")
    
    contacts_filename = f"contacts_{phone_number}.csv"
    
    try:
        result = await client(GetContactsRequest(hash=0))
        contacts = result.contacts
        users = {user.id: user for user in result.users}

        with open(contacts_filename, "w", encoding="utf-8-sig", newline='') as csvfile:
            csv_writer = csv.writer(csvfile)
            
            csv_writer.writerow(["Индекс", "Имя", "Телефон", "Username", "ID"])
            
            for i, contact in enumerate(contacts):
                user = users.get(contact.user_id, None)
                
                if isinstance(user, User):
                    name_parts = []
                    if user.first_name:
                        name_parts.append(user.first_name)
                    if user.last_name:
                        name_parts.append(user.last_name)
                    name = " ".join(name_parts) if name_parts else "Без имени"
                    
                    phone = user.phone or "Скрыт"
                    username = f"@{user.username}" if user.username else "Без username"
                    user_id = user.id
                else:
                    name = "Удаленный пользователь"
                    phone = "Недоступно"
                    username = "Недоступно"
                    user_id = contact.user_id

                csv_writer.writerow([i, name, phone, username, user_id])
                
                contact_info = (
                    f"{i}: {name} | "
                    f"Телефон: {phone} | "
                    f"Username: {username} | "
                    f"ID: {user_id}"
                )
                print(contact_info)

        print(f"\nКонтактов извлечено: {len(contacts)}. Список сохранен в '{contacts_filename}'")
        return contacts

    except Exception as e:
        print(f"Ошибка при получении контактов: {str(e)}")
        return []

async def close_current_session(client):
    print("Закрываю текущую сессию...")
    try:
        await asyncio.sleep(5)
        await delete_telegram_service_messages(client)
        
        await client.log_out()
        print("Текущая сессия успешно закрыта.")
        return True
    except Exception as e:
        print(f"Ошибка при закрытии сессии: {str(e)}")
        try:
            await client.disconnect()
            print("Соединение разорвано, но полностью выйти из аккаунта не удалось.")
        except:
            pass
        return False

async def disconnect_current_session(client):
    print("Отключаюсь от Telegram без выхода из аккаунта...")
    try:
        await client.disconnect()
        print("Соединение закрыто. Сессия сохранена для следующего запуска.")
        return True
    except Exception as e:
        print(f"Ошибка при отключении: {str(e)}")
        return False

async def delete_telegram_service_messages(client):
    print("Пробую удалить последние сервисные сообщения Telegram...")
    try:
        service_entity = None
        async for dialog in client.iter_dialogs():
            if dialog.name == "Telegram" or (hasattr(dialog.entity, 'username') and dialog.entity.username == "telegram"):
                service_entity = dialog.entity
                break
        
        if not service_entity:
            print("Не удалось найти сервисный чат Telegram.")
            return
        
        count = 0
        async for message in client.iter_messages(service_entity, limit=15):
            if not message.text:
                continue
                
            message_text = message.text.lower()
            if any(keyword in message_text for keyword in 
                  ["login code", "código de inicio", "new login", "nuevo inicio", 
                   "new device", "nuevo dispositivo", "detected a login", 
                   "we detected", "hemos detectado", "active sessions", "terminate that session"]):
                try:
                    await client.delete_messages(service_entity, message.id)
                    count += 1
                    print(f"Удалено сервисное сообщение ID: {message.id}")
                except Exception as e:
                    print(f"Не удалось удалить сообщение ID {message.id}: {str(e)}")
        
        print(f"Удалено сервисных сообщений: {count}.")
    except Exception as e:
        print(f"Ошибка при удалении сервисных сообщений: {str(e)}")
        
    await asyncio.sleep(1)

async def main():
    api_id, api_hash, phone_number = get_telegram_config()
    if not phone_number:
        phone_number = input("Введите номер телефона: ")

    client = TelegramClient(phone_number, api_id, api_hash, receive_updates=False)
    
    await client.start(phone=phone_number)
    me = await client.get_me()
    print(f"Сессия запущена от имени {me.first_name}")
    
    await delete_telegram_service_messages(client)
    
    await get_contacts(client, phone_number)

    entities = {
        "Пользователи": [],
        "Каналы": [],
        "Супергруппы": [],
        "Группы": [],
        "Неизвестно": []
    }

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, User):
            entity_type = "Пользователи"
            name = entity.first_name
        elif isinstance(entity, Channel):
            entity_type = "Каналы" if entity.broadcast else "Супергруппы"
            name = entity.title
        elif isinstance(entity, Chat):
            entity_type = "Группы"
            name = entity.title
        elif isinstance(entity, ChannelForbidden):
            entity_type = "Неизвестно"
            name = f"ID: {entity.id}"
        else:
            entity_type = "Неизвестно"
            name = f"ID: {entity.id}"

        register_entity_backup_dir(entity.id, entity)
        
        entities[entity_type].append((entity.id, name, entity))

    entities_filename = f"entities_{phone_number}.csv"
    
    with open(entities_filename, "w", encoding="utf-8-sig", newline='') as csvfile:
        csv_writer = csv.writer(csvfile)
        
        csv_writer.writerow(["Индекс", "Тип", "Название", "ID"])
        
        index = 0
        for category, entity_list in entities.items():
            print(f"\n{category}:")
            
            for id, name, _ in entity_list:
                csv_writer.writerow([index, category, name, id])
                
                line = f"{index}: {name} (ID: {id})"
                if category == "Неизвестно":
                    print(f"\033[1m{line}\033[0m")  
                else:
                    print(line)
                index += 1

    print(f"\nСписок диалогов сохранен в '{entities_filename}'")

    while True:
        choice = input("\nЧто сделать?\n[E] Обработать выбранный диалог\n[T] Обработать все диалоги\n[U] Обновить существующий бэкап\n[D] Удалить сервисные сообщения Telegram\n[X] Закрыть текущую сессию\n[S] Выйти\nВариант: ").lower()
        
        if choice == 'e':
            selected_index = int(input("Введите номер диалога для обработки: "))
            flat_entities = [entity for category in entities.values() for entity in category]
            limit = input("Сколько сообщений получить? (Enter — все): ")
            limit = int(limit) if limit.isdigit() else None
            download_media = is_yes(input("Скачивать медиафайлы? (y/n): "))
            await process_entity(client, *flat_entities[selected_index], limit=limit, download_media=download_media)
        elif choice == 't':
            limit = input("Сколько сообщений получить для каждого диалога? (Enter — все): ")
            limit = int(limit) if limit.isdigit() else None
            download_media = is_yes(input("Скачивать медиафайлы? (y/n): "))
            
            for category in entities.values():
                for entity in category:
                    await process_entity(client, *entity, limit=limit, download_media=download_media)
        elif choice == 'u':
            selected_index = int(input("Введите номер диалога для обновления: "))
            flat_entities = [entity for category in entities.values() for entity in category]
            download_media = is_yes(input("Скачивать медиафайлы? (y/n): "))
            await update_entity(client, *flat_entities[selected_index], download_media=download_media)
        elif choice == 'd':
            await delete_telegram_service_messages(client)
        elif choice == 'x':
            session_closed = await close_current_session(client)
            if session_closed:
                print("Программа завершена после закрытия сессии.")
                return
        elif choice == 's':
            print("\nОтключаюсь перед выходом...")
            await disconnect_current_session(client)
            break

        if choice != 's':
            continue_processing = input("\nВыполнить еще одну операцию? (y/n): ")
            if not is_yes(continue_processing):
                print("\nОтключаюсь перед выходом...")
                await disconnect_current_session(client)
                break

    print("Программа завершена. Спасибо за использование TelegramBackup!")
    
    if client.is_connected():
        print("Отключаюсь перед выходом...")
        await disconnect_current_session(client)

async def media_exists(cursor, entity_id, message_id, media_type):
    cursor.execute("SELECT media_file FROM messages WHERE id = ? AND entity_id = ? AND media_type = ?", 
                 (message_id, entity_id, media_type))
    result = cursor.fetchone()
    if result is None or result[0] is None:
        return False

    media_file = result[0]
    return resolve_media_path(media_file, entity_id) is not None

def _set_file_date(file_path, message):
    try:
        if hasattr(message, 'date') and message.date:
            mtime = message.date.timestamp()
            os.utime(file_path, (mtime, mtime))
    except Exception as e:
        print(f"Ошибка при установке даты файла {file_path}: {e}")

async def download_message_media(message, cursor, entity_id, media_type):
    cursor.execute("SELECT media_file, media_hash FROM messages WHERE id = ? AND entity_id = ?",
                  (message.id, entity_id))
    result = cursor.fetchone()
    if result and result[0]:
        existing_file = result[0]
        existing_media_path = resolve_media_path(existing_file, entity_id)
        if existing_media_path:
            _set_file_date(existing_media_path, message)
            return get_media_db_path(existing_media_path, entity_id), result[1] or get_file_hash(existing_media_path)

    media_file = get_media_target_path(message, entity_id, media_type)
    if os.path.exists(media_file):
        expected_size = getattr(message.file, 'size', None) if getattr(message, 'file', None) else None
        if expected_size is not None and os.path.getsize(media_file) != expected_size:
            print(f"Файл {media_file} имеет неверный размер (битый). Скачиваем заново...")
        else:
            _set_file_date(media_file, message)
            return get_media_db_path(media_file, entity_id), get_file_hash(media_file)

    try:
        os.makedirs(os.path.dirname(media_file), exist_ok=True)
        temp_media_file = media_file + ".download"
        
        # Удаляем временный файл от предыдущей неудачной попытки, если он есть
        if os.path.exists(temp_media_file):
            os.remove(temp_media_file)
            
        downloaded_file = await message.download_media(file=temp_media_file)
        if downloaded_file:
            os.replace(downloaded_file, media_file)
            _set_file_date(media_file, message)
            return get_media_db_path(media_file, entity_id), get_file_hash(media_file)
    except Exception as e:
        print(f"Ошибка при скачивании медиа из сообщения {message.id}: {e}")

    return None, None

async def get_web_preview_data(message):
    preview_data = {
        'title': None,
        'description': None,
        'url': None,
        'site_name': None,
        'image_url': None
    }
    
    if hasattr(message, 'web_preview') and message.web_preview:
        if hasattr(message.web_preview, 'title'):
            preview_data['title'] = message.web_preview.title
        if hasattr(message.web_preview, 'description'):
            preview_data['description'] = message.web_preview.description
        if hasattr(message.web_preview, 'url'):
            preview_data['url'] = message.web_preview.url
        if hasattr(message.web_preview, 'site_name'):
            preview_data['site_name'] = message.web_preview.site_name
        if hasattr(message.web_preview, 'image'):
            preview_data['image_url'] = message.web_preview.image
    
    elif isinstance(message.media, MessageMediaWebPage) and message.media.webpage:
        webpage = message.media.webpage
        if hasattr(webpage, 'title'):
            preview_data['title'] = webpage.title
        if hasattr(webpage, 'description'):
            preview_data['description'] = webpage.description
        if hasattr(webpage, 'url'):
            preview_data['url'] = webpage.url
        if hasattr(webpage, 'site_name'):
            preview_data['site_name'] = webpage.site_name
        if hasattr(webpage, 'photo'):
            preview_data['image_url'] = "web_preview_photo"
    
    return json.dumps(preview_data) if any(preview_data.values()) else None

def get_emoji_string(reaction):
    try:
        if hasattr(reaction, 'emoticon'):
            return reaction.emoticon
        elif hasattr(reaction, 'document_id'):
            return f"CustomEmoji:{reaction.document_id}"
        elif hasattr(reaction, 'emoji'):
            return reaction.emoji
        elif hasattr(reaction, 'reaction'):
            if isinstance(reaction.reaction, str):
                return reaction.reaction
            return get_emoji_string(reaction.reaction)
        elif isinstance(reaction, str):
            return reaction
        else:
            return str(reaction)
    except Exception as e:
        print(f"Ошибка при обработке реакции: {e}")
        return "Неизвестно"

async def get_channel_name_from_message(client, message):
    try:
        if hasattr(message, 'peer_id') and message.peer_id:
            channel_entity = await client.get_entity(message.peer_id)
            if hasattr(channel_entity, 'title'):
                return channel_entity.title
    except Exception as e:
        print(f"Ошибка при получении названия канала: {str(e)}")
    return None

async def process_entity(client, entity_id, entity_name, entity, limit=None, download_media=False):
    print(f"\nОбрабатываю: {entity_name} (ID: {entity_id})")
    register_entity_backup_dir(entity_id, entity)
    
    if isinstance(entity, ChannelForbidden):
        print(f"Диалог {entity_name} (ID: {entity_id}) недоступен. Возможно, он удален или у вас нет прав доступа.")
        return

    sanitized_name = sanitize_filename(f"{entity_id}_{entity_name}")
    db_name = prepare_entity_db(entity_id, sanitized_name)
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER,
        entity_id INTEGER,
        date TEXT,
        text TEXT,
        media_type TEXT,
        media_file TEXT,
        media_hash TEXT,
        forwarded TEXT,
        from_id TEXT,
        views INTEGER,
        sender_name TEXT,
        reply_to_msg_id INTEGER,
        reactions TEXT,
        web_preview TEXT,
        extraction_time TEXT,
        is_service_message BOOLEAN,
        is_voice_message BOOLEAN,
        is_pinned BOOLEAN,
        user_id TEXT,
        PRIMARY KEY (id, entity_id)
    )""")
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS buttons (
        message_id INTEGER,
        entity_id INTEGER,
        row INTEGER,
        column INTEGER,
        text TEXT,
        data TEXT,
        url TEXT,
        UNIQUE(message_id, entity_id, row, column)
    )""")
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS replies (
        message_id INTEGER,
        entity_id INTEGER,
        reply_to_msg_id INTEGER,
        quote_text TEXT,
        UNIQUE(message_id, entity_id)
    )""")
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reactions (
        message_id INTEGER,
        entity_id INTEGER,
        emoji TEXT,
        count INTEGER,
        UNIQUE(message_id, entity_id, emoji)
    )""")
    
    extraction_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        async for message in client.iter_messages(entity, limit=limit):
            message_dict = message.to_dict()
            id = message_dict["id"]
            date = message_dict["date"].isoformat()
            text = message_dict.get("message", None)
            media_type = None
            media_file = None
            media_hash = None
            is_service_message = False
            is_voice_message = False
            is_pinned = message.pinned
            
            if hasattr(message, 'action') and message.action:
                action_dict = message.action.to_dict()
                action_type = action_dict["_"]
                
                if action_type == "MessageActionChatAddUser":
                    user_ids = action_dict.get("users", [])
                    user_names = []
                    for user_id in user_ids:
                        try:
                            user = await client.get_entity(user_id)
                            if hasattr(user, "first_name") and user.first_name:
                                name = user.first_name
                                if hasattr(user, "last_name") and user.last_name:
                                    name += f" {user.last_name}"
                            else:
                                name = f"Пользователь {user_id}"
                            user_names.append(name)
                        except Exception as e:
                            print(f"Ошибка при получении пользователя {user_id}: {str(e)}")
                            user_names.append(f"Пользователь {user_id}")
                    text = f"<service>{', '.join(filter(None, user_names))} присоединился(ась) к группе</service>"
                    is_service_message = True
                elif action_type == "MessageActionChatDeleteUser":
                    user_id = action_dict.get("user_id")
                    try:
                        user = await client.get_entity(user_id)
                        if hasattr(user, "first_name") and user.first_name:
                            name = user.first_name
                            if hasattr(user, "last_name") and user.last_name:
                                name += f" {user.last_name}"
                        else:
                            name = f"Пользователь {user_id}"
                    except Exception as e:
                        print(f"Ошибка при получении пользователя {user_id}: {str(e)}")
                        name = f"Пользователь {user_id}"
                    text = f"<service>{name} покинул(а) группу</service>"
                    is_service_message = True
                elif action_type == "MessageActionChatJoinedByLink":
                    try:
                        if message.sender:
                            user_name = message.sender.first_name
                            if hasattr(message.sender, "last_name") and message.sender.last_name:
                                user_name += f" {message.sender.last_name}"
                        else:
                            user_name = "Кто-то"
                    except:
                        user_name = "Кто-то"
                    text = f"<service>{user_name} присоединился(ась) по пригласительной ссылке</service>"
                    is_service_message = True
                elif action_type == "MessageActionChannelCreate":
                    title = action_dict.get("title", "этот канал")
                    text = f"<service>Канал {title} создан</service>"
                    is_service_message = True
                elif action_type == "MessageActionChatCreate":
                    title = action_dict.get("title", "эта группа")
                    text = f"<service>Группа {title} создана</service>"
                    is_service_message = True
                elif action_type == "MessageActionGroupCall":
                    if action_dict.get("duration"):
                        text = f"<service>Групповой звонок завершен</service>"
                    else:
                        text = f"<service>Групповой звонок начат</service>"
                    is_service_message = True
                elif action_type == "MessageActionChatEditTitle":
                    title = action_dict.get("title", "")
                    text = f"<service>Название группы изменено на: {title}</service>"
                    is_service_message = True
                else:
                    text = f"<service>Сервисное сообщение: {action_type}</service>"
                    is_service_message = True
            
            web_preview = await get_web_preview_data(message)
            
            if message.media:
                media_type = message_dict["media"]["_"]
                
                if media_type == "MessageMediaDocument":
                    if hasattr(message.media, "document") and hasattr(message.media.document, "attributes"):
                        for attr in message.media.document.attributes:
                            if hasattr(attr, "_") and attr._ == "DocumentAttributeAudio":
                                if hasattr(attr, "voice") and attr.voice:
                                    is_voice_message = True
                
                if download_media and is_downloadable_media(message):
                    if not await media_exists(cursor, entity_id, id, media_type):
                        media_file, media_hash = await download_message_media(message, cursor, entity_id, media_type)
                    else:
                        cursor.execute("SELECT media_file, media_hash FROM messages WHERE id = ? AND entity_id = ?", 
                                      (id, entity_id))
                        result = cursor.fetchone()
                        if result:
                            media_file, media_hash = result
            
            forwarded = str(message.fwd_from) if message.fwd_from else None
            from_id = str(message.from_id)
            user_id = extract_user_id(from_id)
            views = message.views
            
            sender_name = None
            
            if message.sender:
                if hasattr(message.sender, 'first_name') and message.sender.first_name:
                    sender_name = message.sender.first_name
                    if hasattr(message.sender, 'last_name') and message.sender.last_name:
                        sender_name += f" {message.sender.last_name}"
                elif hasattr(message.sender, 'title'):
                    sender_name = message.sender.title
            
            if not sender_name:
                try:
                    channel_name = await get_channel_name_from_message(client, message)
                    if channel_name:
                        sender_name = channel_name
                    elif message.fwd_from:
                        if hasattr(message.fwd_from, 'from_name') and message.fwd_from.from_name:
                            sender_name = message.fwd_from.from_name
                        elif message.fwd_from.channel_id:
                            try:
                                fwd_channel = await client.get_entity(message.fwd_from.channel_id)
                                if hasattr(fwd_channel, 'title'):
                                    sender_name = f"{fwd_channel.title} (переслано)"
                            except:
                                pass
                except Exception as e:
                    print(f"Ошибка при определении отправителя сообщения {id}: {e}")
            
            reply_to_msg_id = message.reply_to_msg_id if message.reply_to_msg_id else None
            quote_text = None
            
            if hasattr(message, 'reply_to') and message.reply_to:
                if hasattr(message.reply_to, 'quote_text'):
                    quote_text = message.reply_to.quote_text
            
            reactions_json = None
            if hasattr(message, 'reactions') and message.reactions:
                reactions_list = []
                for reaction in message.reactions.results:
                    emoji = get_emoji_string(reaction.reaction)
                    count = reaction.count
                    reactions_list.append({"emoji": emoji, "count": count})
                    cursor.execute("INSERT OR IGNORE INTO reactions VALUES (?, ?, ?, ?)",
                                  (int(id), int(entity_id), str(emoji), int(count)))
                reactions_json = json.dumps(reactions_list)
            
            cursor.execute("""
            INSERT OR IGNORE INTO messages 
            (id, entity_id, date, text, media_type, media_file, media_hash, forwarded, from_id, views, 
            sender_name, reply_to_msg_id, reactions, web_preview, extraction_time, is_service_message,
            is_voice_message, is_pinned, user_id) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (int(id), int(entity_id), date, text, media_type, media_file, media_hash, forwarded, from_id, 
                 views if views is not None else 0, sender_name, 
                 int(reply_to_msg_id) if reply_to_msg_id is not None else None, 
                 reactions_json, web_preview, extraction_time, is_service_message, is_voice_message, is_pinned, user_id))

            if media_file:
                cursor.execute("""
                UPDATE messages
                SET media_type = ?, media_file = ?, media_hash = ?
                WHERE id = ? AND entity_id = ?
                """, (media_type, media_file, media_hash, int(id), int(entity_id)))
            
            if reply_to_msg_id:
                cursor.execute("INSERT OR IGNORE INTO replies VALUES (?, ?, ?, ?)",
                              (int(id), int(entity_id), int(reply_to_msg_id), quote_text))
            
            if message.buttons:
                for i, row in enumerate(message.buttons):
                    for j, button in enumerate(row):
                        cursor.execute("INSERT OR IGNORE INTO buttons VALUES (?, ?, ?, ?, ?, ?, ?)",
                                       (int(id), int(entity_id), int(i), int(j), str(button.text), 
                                        str(button.data) if button.data else None, 
                                        str(button.url) if button.url else None))
            
            if text and not is_service_message:
                soup = BeautifulSoup(text, "html.parser")
                for link in soup.find_all('a'):
                    cursor.execute("INSERT OR IGNORE INTO buttons VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (int(id), int(entity_id), 0, 0, str(link.text), None, str(link['href'])))
            
            conn.commit()
            
            print(f"Сообщение {id} обработано", end='\r')
        
        print(f"\nВсе сообщения из {entity_name} обработаны.")
    except errors.FloodWaitError as e:
        print(f'Сработало ограничение FloodWait. Жду {e.seconds} секунд перед продолжением.')
        await asyncio.sleep(e.seconds)
    except errors.ChannelPrivateError:
        print(f"Нет доступа к диалогу {entity_name} (ID: {entity_id}). Возможно, он приватный или доступ заблокирован.")
    finally:
        conn.close()
    
    generate_html(db_name, sanitized_name, entity_id)

async def update_entity(client, entity_id, entity_name, entity, download_media=False):
    print(f"\nОбновляю: {entity_name} (ID: {entity_id})")
    register_entity_backup_dir(entity_id, entity)
    
    sanitized_name = sanitize_filename(f"{entity_id}_{entity_name}")
    db_name = prepare_entity_db(entity_id, sanitized_name)
    
    if not os.path.exists(db_name):
        print(f"Для {entity_name} не найдена существующая база. Создаю новый бэкап...")
        await process_entity(client, entity_id, entity_name, entity, download_media=download_media)
        return
    
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
    if not cursor.fetchone():
        print("База существует, но имеет неверную структуру. Создаю новый бэкап...")
        conn.close()
        await process_entity(client, entity_id, entity_name, entity, download_media=download_media)
        return
    
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'")
    table_schema = cursor.fetchone()[0]
    
    if 'is_service_message' not in table_schema:
        print("Обновляю схему базы: добавляю данные о сервисных сообщениях...")
        cursor.execute("ALTER TABLE messages ADD COLUMN is_service_message BOOLEAN DEFAULT 0")
    
    if 'is_voice_message' not in table_schema:
        print("Обновляю схему базы: добавляю данные о голосовых сообщениях...")
        cursor.execute("ALTER TABLE messages ADD COLUMN is_voice_message BOOLEAN DEFAULT 0")
    
    if 'is_pinned' not in table_schema:
        print("Обновляю схему базы: добавляю данные о закрепленных сообщениях...")
        cursor.execute("ALTER TABLE messages ADD COLUMN is_pinned BOOLEAN DEFAULT 0")
        
    if 'user_id' not in table_schema:
        print("Обновляю схему базы: добавляю очищенный ID пользователя...")
        cursor.execute("ALTER TABLE messages ADD COLUMN user_id TEXT")
        
        print("Обрабатываю существующие записи для извлечения ID пользователей...")
        cursor.execute("SELECT id, entity_id, from_id FROM messages")
        for row in cursor.fetchall():
            msg_id, entity_id, from_id = row
            user_id = extract_user_id(from_id)
            if user_id:
                cursor.execute("UPDATE messages SET user_id = ? WHERE id = ? AND entity_id = ?", 
                              (user_id, msg_id, entity_id))
    
    conn.commit()
    
    # Проверяем, есть ли в таблице replies колонка quote_text.
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='replies'")
    replies_schema = cursor.fetchone()
    
    if replies_schema:
        if 'quote_text' not in replies_schema[0]:
            print("Обновляю таблицу replies: добавляю текст цитаты...")
            cursor.execute("ALTER TABLE replies ADD COLUMN quote_text TEXT")
            conn.commit()
    else:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS replies (
            message_id INTEGER,
            entity_id INTEGER,
            reply_to_msg_id INTEGER,
            quote_text TEXT,
            UNIQUE(message_id, entity_id)
        )""")
        conn.commit()
    
    extraction_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    cursor.execute("SELECT MAX(id) FROM messages WHERE entity_id = ?", (entity_id,))
    result = cursor.fetchone()
    last_msg_id = result[0] if result[0] is not None else 0
    
    print(f"Последнее сообщение в базе: {last_msg_id}")
    print("Получаю сообщения. Существующие записи будут пропущены, но сканирование продолжится для заполнения пропусков.")
    
    new_messages_count = 0
    
    try:
        async for message in client.iter_messages(entity):
            cursor.execute("SELECT 1 FROM messages WHERE id = ? AND entity_id = ?", (message.id, entity_id))
            message_already_saved = cursor.fetchone() is not None
                
            message_dict = message.to_dict()
            id = message_dict["id"]
            date = message_dict["date"].isoformat()
            text = message_dict.get("message", None)
            media_type = None
            media_file = None
            media_hash = None
            is_service_message = False
            is_voice_message = False
            is_pinned = message.pinned
            
            if hasattr(message, 'action') and message.action:
                action_dict = message.action.to_dict()
                action_type = action_dict["_"]
                
                if action_type == "MessageActionChatAddUser":
                    user_ids = action_dict.get("users", [])
                    user_names = []
                    for user_id in user_ids:
                        try:
                            user = await client.get_entity(user_id)
                            if hasattr(user, "first_name") and user.first_name:
                                name = user.first_name
                                if hasattr(user, "last_name") and user.last_name:
                                    name += f" {user.last_name}"
                            else:
                                name = f"Пользователь {user_id}"
                            user_names.append(name)
                        except Exception as e:
                            print(f"Ошибка при получении пользователя {user_id}: {str(e)}")
                            user_names.append(f"Пользователь {user_id}")
                    text = f"<service>{', '.join(filter(None, user_names))} присоединился(ась) к группе</service>"
                    is_service_message = True
                elif action_type == "MessageActionChatDeleteUser":
                    user_id = action_dict.get("user_id")
                    try:
                        user = await client.get_entity(user_id)
                        if hasattr(user, "first_name") and user.first_name:
                            name = user.first_name
                            if hasattr(user, "last_name") and user.last_name:
                                name += f" {user.last_name}"
                        else:
                            name = f"Пользователь {user_id}"
                    except Exception as e:
                        print(f"Ошибка при получении пользователя {user_id}: {str(e)}")
                        name = f"Пользователь {user_id}"
                    text = f"<service>{name} покинул(а) группу</service>"
                    is_service_message = True
                elif action_type == "MessageActionChatJoinedByLink":
                    try:
                        if message.sender:
                            user_name = message.sender.first_name
                            if hasattr(message.sender, "last_name") and message.sender.last_name:
                                user_name += f" {message.sender.last_name}"
                        else:
                            user_name = "Кто-то"
                    except:
                        user_name = "Кто-то"
                    text = f"<service>{user_name} присоединился(ась) по пригласительной ссылке</service>"
                    is_service_message = True
                elif action_type == "MessageActionChannelCreate":
                    title = action_dict.get("title", "этот канал")
                    text = f"<service>Канал {title} создан</service>"
                    is_service_message = True
                elif action_type == "MessageActionChatCreate":
                    title = action_dict.get("title", "эта группа")
                    text = f"<service>Группа {title} создана</service>"
                    is_service_message = True
                elif action_type == "MessageActionGroupCall":
                    if action_dict.get("duration"):
                        text = f"<service>Групповой звонок завершен</service>"
                    else:
                        text = f"<service>Групповой звонок начат</service>"
                    is_service_message = True
                elif action_type == "MessageActionChatEditTitle":
                    title = action_dict.get("title", "")
                    text = f"<service>Название группы изменено на: {title}</service>"
                    is_service_message = True
                else:
                    text = f"<service>Сервисное сообщение: {action_type}</service>"
                    is_service_message = True
            
            web_preview = await get_web_preview_data(message)
            
            if message.media:
                media_type = message_dict["media"]["_"]
                
                if media_type == "MessageMediaDocument":
                    if hasattr(message.media, "document") and hasattr(message.media.document, "attributes"):
                        for attr in message.media.document.attributes:
                            if hasattr(attr, "_") and attr._ == "DocumentAttributeAudio":
                                if hasattr(attr, "voice") and attr.voice:
                                    is_voice_message = True
                
                if download_media and is_downloadable_media(message):
                    if not await media_exists(cursor, entity_id, id, media_type):
                        media_file, media_hash = await download_message_media(message, cursor, entity_id, media_type)

            if message_already_saved:
                if download_media and media_file:
                    cursor.execute("""
                    UPDATE messages
                    SET media_type = ?, media_file = ?, media_hash = ?
                    WHERE id = ? AND entity_id = ?
                    """, (media_type, media_file, media_hash, int(id), int(entity_id)))
                    conn.commit()
                    print(f"Медиа сообщения {id} проверено", end='\r')
                else:
                    print(f"Сообщение {id} уже есть, сканирую более старые", end='\r')
                continue
            
            forwarded = str(message.fwd_from) if message.fwd_from else None
            from_id = str(message.from_id)
            user_id = extract_user_id(from_id)
            views = message.views
            
            sender_name = None
            
            if message.sender:
                if hasattr(message.sender, 'first_name') and message.sender.first_name:
                    sender_name = message.sender.first_name
                    if hasattr(message.sender, 'last_name') and message.sender.last_name:
                        sender_name += f" {message.sender.last_name}"
                elif hasattr(message.sender, 'title'):
                    sender_name = message.sender.title
            
            if not sender_name:
                try:
                    channel_name = await get_channel_name_from_message(client, message)
                    if channel_name:
                        sender_name = channel_name
                    elif message.fwd_from:
                        if hasattr(message.fwd_from, 'from_name') and message.fwd_from.from_name:
                            sender_name = message.fwd_from.from_name
                        elif message.fwd_from.channel_id:
                            try:
                                fwd_channel = await client.get_entity(message.fwd_from.channel_id)
                                if hasattr(fwd_channel, 'title'):
                                    sender_name = f"{fwd_channel.title} (переслано)"
                            except:
                                pass
                except Exception as e:
                    print(f"Ошибка при определении отправителя сообщения {id}: {e}")
            
            reply_to_msg_id = message.reply_to_msg_id if message.reply_to_msg_id else None
            quote_text = None
            
            if hasattr(message, 'reply_to') and message.reply_to:
                if hasattr(message.reply_to, 'quote_text'):
                    quote_text = message.reply_to.quote_text
            
            reactions_json = None
            if hasattr(message, 'reactions') and message.reactions:
                reactions_list = []
                for reaction in message.reactions.results:
                    emoji = get_emoji_string(reaction.reaction)
                    count = reaction.count
                    reactions_list.append({"emoji": emoji, "count": count})
                    cursor.execute("INSERT OR IGNORE INTO reactions VALUES (?, ?, ?, ?)",
                                  (int(id), int(entity_id), str(emoji), int(count)))
                reactions_json = json.dumps(reactions_list)
            
            cursor.execute("""
            INSERT OR IGNORE INTO messages 
            (id, entity_id, date, text, media_type, media_file, media_hash, forwarded, from_id, views, 
            sender_name, reply_to_msg_id, reactions, web_preview, extraction_time, is_service_message,
            is_voice_message, is_pinned, user_id) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (int(id), int(entity_id), date, text, media_type, media_file, media_hash, forwarded, from_id, 
                 views if views is not None else 0, sender_name, 
                 int(reply_to_msg_id) if reply_to_msg_id is not None else None, 
                 reactions_json, web_preview, extraction_time, is_service_message, is_voice_message, is_pinned, user_id))

            if media_file:
                cursor.execute("""
                UPDATE messages
                SET media_type = ?, media_file = ?, media_hash = ?
                WHERE id = ? AND entity_id = ?
                """, (media_type, media_file, media_hash, int(id), int(entity_id)))
            
            if reply_to_msg_id:
                cursor.execute("INSERT OR REPLACE INTO replies VALUES (?, ?, ?, ?)",
                              (int(id), int(entity_id), int(reply_to_msg_id), quote_text))
            
            if message.buttons:
                for i, row in enumerate(message.buttons):
                    for j, button in enumerate(row):
                        cursor.execute("INSERT OR IGNORE INTO buttons VALUES (?, ?, ?, ?, ?, ?, ?)",
                                       (int(id), int(entity_id), int(i), int(j), str(button.text), 
                                        str(button.data) if button.data else None, 
                                        str(button.url) if button.url else None))
            
            if text and not is_service_message:
                soup = BeautifulSoup(text, "html.parser")
                for link in soup.find_all('a'):
                    cursor.execute("INSERT OR IGNORE INTO buttons VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (int(id), int(entity_id), 0, 0, str(link.text), None, str(link['href'])))
            
            conn.commit()
            new_messages_count += 1
            print(f"Сообщение {id} обработано", end='\r')
        
        print(f"\nОбновление завершено. Новых сообщений добавлено в {entity_name}: {new_messages_count}.")
    except Exception as e:
        print(f"Ошибка при обновлении сообщений: {e}")
    finally:
        conn.close()
    
    generate_html(db_name, sanitized_name, entity_id)

def generate_html(db_name, chat_name, entity_id=None):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    
    entity_filter = ""
    params = ()
    
    if entity_id is not None:
        entity_filter = "WHERE m.entity_id = ?"
        params = (entity_id,)
    
    cursor.execute(f"""
    SELECT m.id, m.date, m.text, m.media_type, m.media_file, m.forwarded, m.from_id, m.views, 
           m.sender_name, m.reply_to_msg_id, m.reactions, m.entity_id, m.web_preview,
           GROUP_CONCAT(b.text || ',' || b.url, '|') as buttons,
           GROUP_CONCAT(r.emoji || ':' || r.count, ',') as reactions,
           m.is_service_message, m.is_voice_message, m.is_pinned, m.user_id
    FROM messages m 
    LEFT JOIN buttons b ON m.id = b.message_id AND m.entity_id = b.entity_id
    LEFT JOIN reactions r ON m.id = r.message_id AND m.entity_id = r.entity_id
    {entity_filter}
    GROUP BY m.id, m.entity_id
    ORDER BY m.date DESC
    """, params)
    messages = cursor.fetchall()
    messages = [normalize_message_media_path(message, entity_id) for message in messages]
    
    # Словарь для быстрого поиска сообщений по ID.
    message_lookup = {msg[0]: msg for msg in messages}
    
    # Функция для получения сообщения по ID внутри шаблона.
    def get_message_by_id(msg_id):
        try:
            msg_id = int(msg_id)
            return message_lookup.get(msg_id)
        except (ValueError, TypeError):
            return None
    
    # Функция для короткого превью сообщения в блоках ответа.
    def get_reply_preview(msg_id, max_length=30):
        msg = get_message_by_id(msg_id)
        if not msg:
            return "Сообщение не найдено"
        
        sender = msg[8] if msg[8] else "Неизвестно"
        text = msg[2]
        
        if not text:
            if msg[3]:  # Check if it has media
                text = "Медиасообщение"
            elif msg[15]:
                text = "Сервисное сообщение"
            else:
                text = "Пустое сообщение"
        
        if len(text) > max_length:
            text = text[:max_length] + "..."
            
        return f"{sender}: {text}"
    
    if entity_id is not None:
        date_groups = {}
        
        for message in messages:
            date_str = message[1]
            if date_str and 'T' in date_str:
                try:
                    msg_date = datetime.datetime.fromisoformat(date_str)
                    day_str = msg_date.strftime("%B %d, %Y")
                except (ValueError, AttributeError):
                    if 'T' in date_str:
                        day_str = date_str.split('T')[0]
                    else:
                        day_str = "Неизвестная дата"
            else:
                day_str = "Неизвестная дата"
            
            if day_str not in date_groups:
                date_groups[day_str] = []
            
            date_groups[day_str].append(message)
        
        grouped_messages = [(date, msgs) for date, msgs in date_groups.items()]
    else:
        grouped_messages = []
        for message in messages:
            date_str = message[1]
            if date_str and 'T' in date_str:
                day_str = date_str.split('T')[0]
            else:
                day_str = "Неизвестная дата"
            grouped_messages.append((day_str, [message]))
    
    conn.close()

    env = Environment(loader=FileSystemLoader('./'))
    template = env.get_template('template.html')
    
    output = template.render(
        chat_name=chat_name,
        grouped_messages=grouped_messages,
        entity_id=entity_id,
        os=os,
        get_url_from_forwarded=get_url_from_forwarded,
        json=json,
        get_message_by_id=get_message_by_id,
        get_reply_preview=get_reply_preview
    )
    
    output_path = get_entity_html_path(entity_id, chat_name) if entity_id is not None else f"{chat_name}.html"
    if entity_id is not None:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, "w", encoding='utf-8') as f:
        f.write(output)
    
    print(f"HTML-файл создан: {output_path}")

if __name__ == "__main__":
    asyncio.run(main())
