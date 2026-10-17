import asyncio, io, logging, os, re, sqlite3, sys
from aiohttp import web
from telethon import TelegramClient, events, types
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import FloodWaitError
import pypdf

logging.basicConfig(format="%(asctime)s - [%(levelname)s]: %(message)s", level=logging.INFO)
logger = logging.getLogger("render_bot")

API_ID = int(os.getenv("TELEGRAM_API_ID", "36822080"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "430634fb7d9e5955d7ee76ed8c826ebb")
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
PORT = int(os.getenv("PORT", "10000"))

if not SESSION_STRING:
    logger.error("TELEGRAM_SESSION_STRING environment variable is missing!")
    sys.exit(1)

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# --- SQLite Database Initialization ---
def init_db():
    conn = sqlite3.connect("academic_index.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS indexed_files (
            chat_id INTEGER, message_id INTEGER, chat_title TEXT, file_name TEXT,
            total_pages INTEGER, PRIMARY KEY (chat_id, message_id)
        );
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS pdf_pages_fts USING fts5(
            chat_id UNINDEXED, message_id UNINDEXED, chat_title UNINDEXED,
            file_name, page_num UNINDEXED, text_content, tokenize='porter unicode61'
        );
    """)
    conn.commit()
    conn.close()

init_db()

# --- Advanced Query Parser (Exact Quotes, +, -, AND, OR, NOT) ---
def parse_fts5_query(user_query: str) -> str:
    pattern = r'(-\"[^\"]+\"|-\w+|\"[^\"]+\"|\w+|AND|OR|NOT)'
    matches = re.findall(pattern, user_query.strip())
    if not matches:
        return '""'

    tokens = []
    for m in matches:
        upper = m.upper()
        if upper in ('AND', 'OR', 'NOT'):
            tokens.append(upper)
        elif m.startswith('-'):
            clean = m[1:].strip('"')
            tokens.extend(['NOT', f'"{clean}"'])
        elif m.startswith('+'):
            clean = m[1:].strip('"')
            tokens.extend(['AND', f'"{clean}"'])
        elif m.startswith('"') and m.endswith('"'):
            tokens.append(m)
        else:
            tokens.append(f'"{m}"')

    clean_terms = []
    for t in tokens:
        if t in ('AND', 'OR', 'NOT'):
            if clean_terms and clean_terms[-1] not in ('AND', 'OR', 'NOT'):
                clean_terms.append(t)
        else:
            if clean_terms and clean_terms[-1] not in ('AND', 'OR', 'NOT'):
                clean_terms.append('AND')
            clean_terms.append(t)

    while clean_terms and clean_terms[-1] in ('AND', 'OR', 'NOT'):
        clean_terms.pop()

    return ' '.join(clean_terms) if clean_terms else '""'

def search_pdf_content(query, limit=10):
    fts_expr = parse_fts5_query(query)
    results = []
    with sqlite3.connect("academic_index.db") as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT chat_id, message_id, chat_title, file_name, page_num,
                       snippet(pdf_pages_fts, 5, '<b>', '</b>', '...', 18)
                FROM pdf_pages_fts WHERE pdf_pages_fts MATCH ? ORDER BY rank LIMIT ?;
            """, (fts_expr, limit))
            for row in cursor.fetchall():
                results.append({"chat_id": int(row[0]), "msg_id": int(row[1]), "title": row[2], "file": row[3], "page": int(row[4]), "snippet": row[5]})
        except Exception as e:
            logger.error(f"FTS Query failed for '{fts_expr}': {e}")
    return results

def get_doc_filename(msg):
    if msg.media and hasattr(msg.media, "document") and msg.media.document:
        for attr in msg.media.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                return attr.file_name
    return None

# --- Telegram Commands ---
@client.on(events.NewMessage(pattern=r"(?i)^[./](qp|find|pyq)\s+(.+)", outgoing=True))
async def on_search(event):
    query = event.pattern_match.group(2).strip()
    status_msg = await event.reply(f"🔍 <b>Filtering across internal PDF pages for:</b> <code>{query}</code>...", parse_mode="html")
    
    deep_matches = search_pdf_content(query)
    forwarded = set()
    count = 0

    if deep_matches:
        for m in deep_matches:
            key = (m["chat_id"], m["msg_id"])
            if key in forwarded: continue
            forwarded.add(key)
            try:
                msg = await client.get_messages(m["chat_id"], ids=m["msg_id"])
                if msg:
                    await event.reply(
                        f"📄 <b>{m['file']}</b> (Page {m['page']})\n"
                        f"📍 <i>{m['title']}</i>\n"
                        f"💬 Snippet: <i>{m['snippet']}</i>",
                        parse_mode="html"
                    )
                    await client.forward_messages("me", msg)
                    count += 1
                    await asyncio.sleep(0.4)
            except Exception: pass

    # Also search regular group message titles/captions
    clean_words = [w for w in re.findall(r'\w+', query) if len(w) >= 3 and w.upper() not in ('AND', 'OR', 'NOT')]
    simple_query = " ".join(clean_words[:2]) if clean_words else query
    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel): continue
        try:
            async for msg in client.iter_messages(dialog, search=simple_query, filter=types.InputMessagesFilterDocument, limit=6):
                key = (dialog.id, msg.id)
                if key in forwarded: continue
                forwarded.add(key)
                await client.forward_messages("me", msg)
                count += 1
                if count >= 12: break
        except Exception: continue

    if count == 0:
        await status_msg.edit(f"❌ No question papers or answers matched <code>{query}</code>.\n💡 <i>Try broader terms or send <code>.index</code> to index new PDFs.</i>", parse_mode="html")
    else:
        await status_msg.edit(f"🏁 Found and forwarded {count} matching resources for <b>{query}</b>.", parse_mode="html")

@client.on(events.NewMessage(pattern=r"(?i)^[./]index", outgoing=True))
async def on_index(event):
    status_msg = await event.reply("⏳ <b>Scanning and deep-indexing all group PDFs...</b>", parse_mode="html")
    total_files = 0
    total_pages = 0

    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel): continue
        chat_title = dialog.name or f"Chat {dialog.id}"
        try:
            async for msg in client.iter_messages(dialog, filter=types.InputMessagesFilterDocument, limit=40):
                fname = get_doc_filename(msg)
                if not fname or not fname.lower().endswith(".pdf"): continue
                if (msg.media.document.size or 0) > 30 * 1024 * 1024: continue

                with sqlite3.connect("academic_index.db") as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT 1 FROM indexed_files WHERE chat_id=? AND message_id=?", (dialog.id, msg.id))
                    if cur.fetchone(): continue

                pdf_bytes = await client.download_media(msg, file=bytes)
                if not pdf_bytes: continue

                try:
                    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
                    with sqlite3.connect("academic_index.db") as conn:
                        p_indexed = 0
                        for idx, page in enumerate(reader.pages, 1):
                            txt = re.sub(r"\s+", " ", page.extract_text() or "").strip()
                            if len(txt) >= 15:
                                conn.execute("INSERT INTO pdf_pages_fts VALUES (?,?,?,?,?,?)", (dialog.id, msg.id, chat_title, fname, idx, txt))
                                p_indexed += 1
                        conn.execute("INSERT INTO indexed_files VALUES (?,?,?,?,?)", (dialog.id, msg.id, chat_title, fname, p_indexed))
                        conn.commit()
                        if p_indexed > 0:
                            total_files += 1
                            total_pages += p_indexed
                except Exception: pass
        except Exception: continue

    await status_msg.edit(f"✅ <b>Indexing Complete!</b>\nIndexed {total_files} new PDFs ({total_pages} pages). You can now search with full AND, OR, NOT filters!", parse_mode="html")

# --- Dummy HTTP Server for Render Health Check ---
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health check listening on port {PORT}")

async def main():
    await start_server()
    await client.start()
    logger.info("Telegram Client connected and active on Render!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
