import asyncio, os, io, logging
from datetime import datetime, timezone
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

import firebase_admin
from firebase_admin import credentials, firestore, storage as fb_storage
from google.cloud import firestore as gcfirestore
from google.cloud import storage as gcs
from google.cloud import build_v1

# =======================
# PRE-SET ENV (safe defaults)
# =======================
# NOTE: Replace REPO_URL with your real repo URL (public or service-account readable).
_PRESET = {
    "GOOGLE_APPLICATION_CREDENTIALS": "/absolute/path/to/service-account.json",  # <-- put your SA json path
    "GCP_PROJECT": "proh-c5886",
    "GCLOUD_PROJECT": "proh-c5886",
    "GCS_BUCKET": "proh-c5886.appspot.com",
    "REPO_URL": "https://github.com/<your>/<android-repo>.git",  # <-- CHANGE this
    "APP_DIR": "app",
    "TELEGRAM_BOT_TOKEN": "7451661904:AAE05YaujmpJQHNqc67lTBsXczL3qosBZSY",   # user-provided
}
for k, v in _PRESET.items():
    os.environ.setdefault(k, v)

# =======================
# CONFIG (reads env, fallback to above)
# =======================
PROJECT_ID  = os.environ.get("GCP_PROJECT") or os.environ.get("GCLOUD_PROJECT") or "proh-c5886"
BUCKET_NAME = os.environ.get("GCS_BUCKET", "proh-c5886.appspot.com")
REPO_URL    = os.environ.get("REPO_URL")
APP_DIR     = os.environ.get("APP_DIR", "app")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# =======================
# LOGGING
# =======================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("apk-bot")

# =======================
# FIREBASE/GOOGLE INIT
# =======================
if not firebase_admin._apps:
    sac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not sac or not os.path.exists(sac):
        raise RuntimeError("Set GOOGLE_APPLICATION_CREDENTIALS to your service-account JSON path.")
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {"storageBucket": BUCKET_NAME})

db: gcfirestore.Client = firestore.client()
bucket = fb_storage.bucket()
gcs_client = gcs.Client(project=PROJECT_ID)
cb_client  = build_v1.services.cloud_build.CloudBuildClient()

# =======================
# HELPERS
# =======================
NAME_RX = r"^[A-Za-z0-9][A-Za-z0-9 _-]{2,40}$"
def valid(n:str)->bool:
    import re; return bool(re.match(NAME_RX, (n or "").strip()))
def infer_project(apk_name:str)->str:
    part=(apk_name or "").split("-")[0]; return (part or apk_name or "DEFAULT").replace(" ","").upper()
def now_ms()->int: return int(datetime.now(tz=timezone.utc).timestamp()*1000)

@firestore.transactional
def tx_incr_apk_seq(tx:gcfirestore.Transaction, project:str)->int:
    ctr=db.collection("counters").document(project); snap=tx.get(ctr)
    last=(snap.get("lastApkSeq") if snap.exists else 0) or 0
    nxt=int(last)+1; tx.set(ctr, {"lastApkSeq":nxt}, merge=True); return nxt

def fetch_template(project:str, preferred_index:Optional[int])->Optional[dict]:
    if preferred_index is not None:
        cur=list(db.collection("json_templates").where("project","==",project).where("index","==",preferred_index).limit(1).stream())
        if cur: return cur[0].to_dict()
    cur=list(db.collection("json_templates").where("project","==",project)
             .order_by("index", direction=gcfirestore.Query.DESCENDING).limit(1).stream())
    return cur[0].to_dict() if cur else None

def build_steps(google_services_json:str, batch_id:str, apk_name:str):
    import base64; b64=base64.b64encode(google_services_json.encode("utf-8")).decode("utf-8")
    return [
        # 1) Clone repo
        build_v1.types.BuildStep(name="gcr.io/cloud-builders/git", args=["clone", REPO_URL, "src"]),
        # 2) Replace google-services.json in app/ (as per your screenshot)
        build_v1.types.BuildStep(
            name="gcr.io/cloud-builders/gcloud", entrypoint="bash",
            args=["-c",
                  f"set -e && cd src/{APP_DIR} && rm -f google-services.json || true && "
                  f"echo {b64} | base64 -d > google-services.json && ls -l google-services.json"]
        ),
        # 3) Build release
        build_v1.types.BuildStep(name="gcr.io/cloud-builders/gradle", dir="src", args=["assembleRelease"]),
        # 4) Upload APK to GCS (no link—bot will fetch bytes)
        build_v1.types.BuildStep(
            name="gcr.io/cloud-builders/gsutil",
            args=["cp", f"src/{APP_DIR}/build/outputs/apk/release/*.apk",
                  f"gs://{BUCKET_NAME}/builds/{batch_id}/app-release.apk"]
        ),
    ]

def start_cloud_build(google_services_json:str, batch_id:str, apk_name:str)->str:
    if not REPO_URL or "<your>/" in REPO_URL:
        raise RuntimeError("Set REPO_URL to your Android repo (public or accessible to the SA).")
    build=build_v1.types.Build(steps=build_steps(google_services_json,batch_id,apk_name), timeout={"seconds":3600})
    req=build_v1.types.CreateBuildRequest(project_id=PROJECT_ID, build=build)
    op=cb_client.create_build(request=req)
    return op.operation.name

async def wait_build(op_name:str)->None:
    from google.api_core.operation import Operation
    op=Operation(cb_client.transport.operations_client, op_name, response_type=build_v1.types.Build)
    loop=asyncio.get_event_loop(); await loop.run_in_executor(None, op.result)

def gcs_download(path:str)->bytes:
    return gcs_client.bucket(BUCKET_NAME).blob(path).download_as_bytes()

# =======================
# TELEGRAM HANDLERS
# =======================
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Namaste! Main APK bot hoon.\n\n"
        "/list → recent batches\n"
        "/confirm <batchId> [project] → next apk no. + JSON pick + build\n"
        "/download <batchId> → built apk bhejta hoon"
    )

async def cmd_help(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

async def cmd_list(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    docs=list(db.collection("apk_files").order_by("createdAt", direction=gcfirestore.Query.DESCENDING).limit(10).stream())
    if not docs: return await update.message.reply_text("Koi batch nahi mila.")
    lines, kb=[],[]
    for d in docs:
        x=d.to_dict(); st=x.get("status","available")
        lines.append(f"• {d.id} | {x.get('apkName')} | status: {st}")
        if st in ("available","waiting_confirm","confirmed"):
            kb.append([InlineKeyboardButton(f"Confirm: {x.get('apkName')}", callback_data=f"confirm:{d.id}")])
        elif st=="built":
            kb.append([InlineKeyboardButton(f"Download: {x.get('apkName')}", callback_data=f"download:{d.id}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def on_button(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer(); data=q.data or ""
    if data.startswith("confirm:"): return await do_confirm_build(q, ctx, data.split(":",1)[1], None)
    if data.startswith("download:"): return await do_download(q, ctx, data.split(":",1)[1])

async def cmd_confirm(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not ctx.args: return await update.message.reply_text("Usage: /confirm <batchId> [project]")
    await do_confirm_build(update, ctx, ctx.args[0], ctx.args[1] if len(ctx.args)>1 else None)

async def do_confirm_build(where, ctx:ContextTypes.DEFAULT_TYPE, batch_id:str, explicit_project:Optional[str]):
    reply = where.edit_message_text if hasattr(where,"edit_message_text") else where.message.reply_text
    ref=db.collection("apk_files").document(batch_id); snap=ref.get()
    if not snap.exists: return await reply(text="Batch nahi mila.")
    x=snap.to_dict(); st=x.get("status","available")
    if st in ("building","built"): return await reply(text=f"Already {st}.")
    apk_name=x.get("apkName") or "(no-name)"
    proj=explicit_project or x.get("project") or infer_project(apk_name)
    ref.set({"status":"confirmed","project":proj}, merge=True)

    # next apk no. + json select
    next_apk = tx_incr_apk_seq(db.transaction(), proj)
    template = fetch_template(proj, next_apk)
    if not template: ref.set({"status":"error","error":"No JSON template"}, merge=True); return await reply(text=f"{proj}: JSON template nahi mila.")
    json_text=(template.get("dataText") or "").strip()
    if not json_text: ref.set({"status":"error","error":"Empty JSON"}, merge=True); return await reply(text="Template empty.")

    await reply(text=f"Build start → project={proj}, apk_seq={next_apk}, json_index={template.get('index')}")
    ref.set({"status":"building","apkSeq":next_apk,"jsonIndexUsed":template.get("index"),"buildStart":now_ms()}, merge=True)

    # Cloud Build
    try: op_name=start_cloud_build(json_text, batch_id, apk_name)
    except Exception as e: ref.set({"status":"error","error":str(e)}, merge=True); return await reply(text=f"Cloud Build start failed: {e}")
    try: await wait_build(op_name)
    except Exception as e: ref.set({"status":"error","error":f"build failed: {e}"}, merge=True); return await reply(text=f"Build failed: {e}")

    # Fetch APK bytes from GCS (no link) and send
    gcs_path=f"builds/{batch_id}/app-release.apk"
    ref.set({"status":"built","gcsPath":gcs_path,"builtAt":now_ms()}, merge=True)
    try: data=gcs_download(gcs_path)
    except Exception as e: return await reply(text=f"Build done but download failed: {e}")

    await where.get_bot().send_document(chat_id=where.effective_chat.id, document=io.BytesIO(data),
        filename=f"{apk_name}-release.apk",
        caption=f"✅ Build complete\nproject={proj} • seq={next_apk} • index={template.get('index')}"
    )

async def cmd_download(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    if not ctx.args: return await update.message.reply_text("Usage: /download <batchId>")
    await do_download(update, ctx, ctx.args[0])

async def do_download(where, ctx:ContextTypes.DEFAULT_TYPE, batch_id:str):
    ref=db.collection("apk_files").document(batch_id); snap=ref.get()
    if not snap.exists: return await where.message.reply_text("Batch nahi mila.")
    x=snap.to_dict()
    if x.get("status")!="built" or not x.get("gcsPath"): return await where.message.reply_text("APK ready nahi hai.")
    try: data=gcs_download(x["gcsPath"])
    except Exception as e: return await where.message.reply_text(f"Download error: {e}")
    await where.get_bot().send_document(chat_id=where.effective_chat.id, document=io.BytesIO(data),
        filename=f"{x.get('apkName','app')}-release.apk", caption="Here you go 👇")

def main():
    if not TELEGRAM_TOKEN: raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var.")
    app=Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("list",  cmd_list))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot started."); app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__": main()
