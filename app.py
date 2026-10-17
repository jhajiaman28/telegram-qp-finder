import asyncio, io, logging, os, re, sqlite3, sys
from aiohttp import web
from telethon import TelegramClient, events, types
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeFilename, InputMessagesFilterDocument, InputPeerEmpty
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.messages import SearchGlobalRequest
from telethon.errors import FloodWaitError
import pypdf

logging.basicConfig(format="%(asctime)s - [%(levelname)s]: %(message)s", level=logging.INFO)
logger = logging.getLogger("academic_bot")

API_ID = int(os.getenv("TELEGRAM_API_ID", "36822080"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "430634fb7d9e5955d7ee76ed8c826ebb")
SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
SESSION_NAME = os.getenv("SESSION_NAME", "qp_finder_session").strip()
PORT = int(os.getenv("PORT", "10000"))

if SESSION_STRING:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

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

def extract_terms(raw_query: str):
    neg_matches = re.findall(r'-(\"[^\"]+\"|\w+)', raw_query.strip())
    negatives = [m.strip('"').lower() for m in neg_matches]
    pos_q = re.sub(r'-(?:\"[^\"]+\"|\w+)', '', raw_query).strip()
    return pos_q, negatives

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

def search_pdf_content(raw_query, limit=10):
    pos_query, neg_terms = extract_terms(raw_query)
    pos_fts = parse_fts5_query(pos_query)
    if not pos_fts or pos_fts == '""':
        return []

    results = []
    with sqlite3.connect("academic_index.db") as conn:
        cursor = conn.cursor()
        disqualified_docs = set()
        if neg_terms:
            neg_fts = " OR ".join([f'"{n}"' for n in neg_terms])
            try:
                cursor.execute("SELECT DISTINCT chat_id, message_id FROM pdf_pages_fts WHERE pdf_pages_fts MATCH ?", (neg_fts,))
                disqualified_docs = {(row[0], row[1]) for row in cursor.fetchall()}
            except Exception:
                pass

        try:
            cursor.execute("""
                SELECT chat_id, message_id, chat_title, file_name, page_num,
                       snippet(pdf_pages_fts, 5, '<b>', '</b>', '...', 18)
                FROM pdf_pages_fts WHERE pdf_pages_fts MATCH ? ORDER BY rank LIMIT 30;
            """, (pos_fts,))
            
            for row in cursor.fetchall():
                key = (int(row[0]), int(row[1]))
                if key in disqualified_docs:
                    continue
                results.append({
                    "chat_id": key[0], "msg_id": key[1], "title": row[2],
                    "file": row[3], "page": int(row[4]), "snippet": row[5]
                })
                if len(results) >= limit:
                    break
        except Exception as e:
            logger.error(f"FTS Query failed: {e}")
            
    return results

def get_doc_filename(msg):
    if msg.media and hasattr(msg.media, "document") and msg.media.document:
        for attr in msg.media.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                return attr.file_name
    return None

# --- Command 1: Start & Help Guide Cheat Sheet ---
@client.on(events.NewMessage(pattern=r"(?i)^[./](start|help)$", outgoing=True))
async def on_start_help(event):
    guide_text = (
        "📚 <b>Academic Question Paper & Channel Finder Bot</b>\n"
        "<i>Command Reference Card</i>\n"
        "───────────────────────────────\n\n"
        "🔍 <b>1. Search Joined Groups & PDF Pages</b>\n"
        "• <code>.qp &lt;topic&gt;</code> or <code>.find &lt;topic&gt;</code>\n"
        "  Searches inside every PDF page and message of your joined groups.\n"
        "  <i>Example:</i> <code>.qp \"Upper Limb\" -MCQ</code>\n"
        "  <i>Example:</i> <code>.find Biochemistry AND Enzymes</code>\n\n"
        "🌐 <b>2. Search Unjoined Public Channels Globally</b>\n"
        "• <code>.global &lt;topic&gt;</code>\n"
        "  Searches public Telegram channels for PDFs (without needing to join them) and forwards them.\n"
        "  <i>Example:</i> <code>.global ABVMU MBBS Question Paper</code>\n"
        "  <i>Example:</i> <code>.global Anatomy Dissection Solved</code>\n\n"
        "📡 <b>3. Discover New Study Channels</b>\n"
        "• <code>.channel &lt;topic&gt;</code>\n"
        "  Discovers public medical channels, question banks, and notes archives.\n"
        "  <i>Example:</i> <code>.channel MBBS Anatomy</code>\n"
        "  <i>Example:</i> <code>.channel Medical PYQ</code>\n\n"
        "⚙️ <b>4. Indexing & Database Management</b>\n"
        "• <code>.index</code> — Scans all joined groups to index text from PDF pages.\n"
        "• <code>.status</code> — Shows how many PDFs, pages, and groups are indexed.\n\n"
        "💡 <b>Search Filters & Operators Cheat Sheet:</b>\n"
        "• <b>Must Only Contain (Exact):</b> <code>\"Upper Limb\"</code> in quotes\n"
        "• <b>Must Also Contain (AND):</b> <code>+</code> or <code>AND</code> (e.g. <code>Anatomy + Thorax</code>)\n"
        "• <b>Either / Or (OR):</b> <code>OR</code> (e.g. <code>Anatomy OR Physiology</code>)\n"
        "• <b>Exclude (NOT):</b> <code>-</code> or <code>NOT</code> (e.g. <code>-MCQ</code> or <code>NOT Genetics</code>)"
    )
    await event.reply(guide_text, parse_mode="html")

# --- Command 2: Deep Local Search (.qp, .find, .pyq) ---
@client.on(events.NewMessage(pattern=r"(?i)^[./](qp|find|pyq)\s+(.+)", outgoing=True))
async def on_search(event):
    query = event.pattern_match.group(2).strip()
    pos_query, neg_terms = extract_terms(query)
    status_msg = await event.reply(f"🔍 <b>Filtering across PDF contents for:</b> <code>{query}</code>...", parse_mode="html")
    
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

    clean_words = [w for w in re.findall(r'\w+', pos_query) if len(w) >= 3 and w.upper() not in ('AND', 'OR', 'NOT')]
    simple_query = " ".join(clean_words[:2]) if clean_words else pos_query
    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel): continue
        try:
            async for msg in client.iter_messages(dialog, search=simple_query, filter=types.InputMessagesFilterDocument, limit=8):
                key = (dialog.id, msg.id)
                if key in forwarded: continue
                fname = get_doc_filename(msg)
                
                caption = msg.message or ""
                search_corpus = f"{fname or ''} {caption}".lower()
                if any(neg in search_corpus for neg in neg_terms):
                    continue

                forwarded.add(key)
                await client.forward_messages("me", msg)
                count += 1
                if count >= 12: break
        except Exception: continue

    if count == 0:
        await status_msg.edit(f"❌ No question papers matched <code>{query}</code>.\n💡 <i>Send <code>.start</code> for command help or <code>.index</code> to index new PDFs.</i>", parse_mode="html")
    else:
        await status_msg.edit(f"🏁 Found and forwarded {count} matching resources for <b>{query}</b>.", parse_mode="html")

# --- Command 3: Global Unjoined Public PDF Search (.global) ---
@client.on(events.NewMessage(pattern=r"(?i)^[./]global\s+(.+)", outgoing=True))
async def on_global_search(event):
    query = event.pattern_match.group(1).strip()
    pos_query, neg_terms = extract_terms(query)
    status_msg = await event.reply(f"🌐 <b>Searching public Telegram channels globally for:</b> <code>{query}</code>...", parse_mode="html")

    try:
        res = await client(SearchGlobalRequest(
            q=pos_query,
            filter=InputMessagesFilterDocument(),
            min_date=None,
            max_date=None,
            offset_rate=0,
            offset_peer=InputPeerEmpty(),
            offset_id=0,
            limit=15
        ))

        chats_dict = {c.id: c for c in getattr(res, "chats", [])}
        forwarded_count = 0

        for msg in getattr(res, "messages", []):
            if not msg.media or not hasattr(msg.media, "document") or not msg.media.document:
                continue

            fname = get_doc_filename(msg) or "Document"
            caption = msg.message or ""
            corpus = f"{fname} {caption}".lower()

            if any(neg in corpus for neg in neg_terms):
                continue

            chat_id = msg.peer_id.channel_id if hasattr(msg.peer_id, "channel_id") else getattr(msg.peer_id, "chat_id", None)
            chat_obj = chats_dict.get(chat_id)
            channel_title = chat_obj.title if chat_obj else "Public Channel"
            username = getattr(chat_obj, "username", None)
            channel_link = f"https://t.me/{username}" if username else "Public"

            await event.reply(
                f"🌐 <b>Global Find:</b> 📄 <code>{fname}</code>\n"
                f"📢 <b>From Channel:</b> <a href=\"{channel_link}\">{channel_title}</a>",
                parse_mode="html",
                link_preview=False
            )
            await client.forward_messages("me", msg)
            forwarded_count += 1
            await asyncio.sleep(0.4)
            if forwarded_count >= 8:
                break

        if forwarded_count == 0:
            await status_msg.edit(f"❌ No public channel PDFs found for <code>{query}</code>.\n💡 <i>Try broader terms like <code>.global Anatomy PYQ</code>.</i>", parse_mode="html")
        else:
            await status_msg.edit(f"🏁 Found and forwarded {forwarded_count} global public PDFs for <b>{query}</b>.", parse_mode="html")

    except Exception as e:
        logger.error(f"Global search error: {e}")
        await status_msg.edit(f"❌ Global search failed: <code>{e}</code>", parse_mode="html")

# --- Command 4: Channel Discoverer (.channel) ---
@client.on(events.NewMessage(pattern=r"(?i)^[./]channel\s+(.+)", outgoing=True))
async def on_channel_discover(event):
    query = event.pattern_match.group(1).strip()
    status_msg = await event.reply(f"📡 <b>Discovering public channels for:</b> <code>{query}</code>...", parse_mode="html")

    try:
        res = await client(SearchRequest(q=query, limit=12))
        channels = [c for c in getattr(res, "chats", []) if getattr(c, "broadcast", False) or getattr(c, "megagroup", False)]

        if not channels:
            await status_msg.edit(f"❌ No public channels found matching <code>{query}</code>.", parse_mode="html")
            return

        lines = [f"📡 <b>Public Channels Found for:</b> <i>{query}</i>\n{'─'*35}"]
        for c in channels[:8]:
            title = c.title or "Channel"
            username = c.username
            count_str = f"👥 {c.participants_count:,} members" if getattr(c, "participants_count", None) else ""
            if username:
                link = f"https://t.me/{username}"
                lines.append(f"• <b><a href=\"{link}\">{title}</a></b> (@{username}) {count_str}")
            else:
                lines.append(f"• <b>{title}</b> {count_str}")

        lines.append("\n👉 <i>Tap any link to browse or join the channel!</i>")
        await status_msg.edit("\n".join(lines), parse_mode="html", link_preview=False)

    except Exception as e:
        logger.error(f"Channel search error: {e}")
        await status_msg.edit(f"❌ Channel discovery failed: <code>{e}</code>", parse_mode="html")

# --- Command 5: Deep Indexer (.index) ---
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

    await status_msg.edit(f"✅ <b>Indexing Complete!</b>\nIndexed {total_files} new PDFs ({total_pages} pages). Send <code>.start</code> for search tips!", parse_mode="html")

# --- Command 6: Database Stats (.status) ---
@client.on(events.NewMessage(pattern=r"(?i)^[./]status", outgoing=True))
async def on_status(event):
    with sqlite3.connect("academic_index.db") as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(total_pages), 0), COUNT(DISTINCT chat_id) FROM indexed_files;")
        row = cur.fetchone()
        tot_files, tot_pages, tot_groups = row[0] or 0, row[1] or 0, row[2] or 0

    size_mb = os.path.getsize("academic_index.db") / (1024 * 1024) if os.path.exists("academic_index.db") else 0
    await event.reply(
        f"📊 <b>Academic Index Database Status:</b>\n"
        f"• <b>Indexed PDFs:</b> {tot_files}\n"
        f"• <b>Indexed Pages:</b> {tot_pages}\n"
        f"• <b>Active Groups:</b> {tot_groups}\n"
        f"• <b>Database Size:</b> {size_mb:.2f} MB\n\n"
        f"<i>Send <code>.index</code> to update or <code>.start</code> for commands!</i>",
        parse_mode="html"
    )

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
    if os.getenv("PORT"):
        await start_server()
    await client.start()
    logger.info("Bot is active and listening for commands!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
