from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatMemberUpdated
import asyncio
import os

# BURAYI DOLDUR
BOT_TOKEN = "8963842011:AAEbr6gqXrAB7WmcAwV2-wd5jjl955H3wlI"
API_ID = 33022086
API_HASH = "733ced5fca357d03f19fe2a055aae225"

# Otomatik algılanan grup ID'si ve son işlenen dosya yolu bu değişkenlerde tutulacak
GROUP_ID = None
LAST_TXT_PATH = None

app = Client(
    "delete_cleaner",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- ANA MENÜ METNİ VE BUTONLARI ---
def get_main_menu():
    text = (
        "👋 **Bot aktif!**\n\n"
        "📌 **Kullanım:**\n"
        "- .txt dosyası gönder (ID veya @username)\n"
        "- /refresh → son dosyayı tekrar işler\n\n"
        "⚙️ **Özellikler:**\n"
        "- ID + @username destekler\n"
        "- Admin/owner korunur\n"
        "- Hatalı kullanıcılar atlanır\n"
        "- Silinmiş hesapları (Deleted Account) temizler\n\n"
        "📩 **Ulaş:** @saklanmam"
    )
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 /deleted", callback_data="menu_deleted")],
        [InlineKeyboardButton("📄 /id den cıkarma", callback_data="menu_id_kick")],
        [InlineKeyboardButton("📩 İletişim: @saklanmam", url="https://t.me/saklanmam")]
    ])
    return text, buttons

# --- OTOMATİK GRUP ALGILAMA SİSTEMİ ---
@app.on_chat_member_updated()
async def track_group(client, chat_member_updated: ChatMemberUpdated):
    global GROUP_ID
    if chat_member_updated.new_chat_member and chat_member_updated.new_chat_member.user.is_self:
        GROUP_ID = chat_member_updated.chat.id
        print(f"🤖 Bot yeni bir gruba eklendi ve ID kaydedildi: {GROUP_ID} ({chat_member_updated.chat.title})")

@app.on_message(filters.private & filters.command("start"))
async def start(client, message):
    text, reply_markup = get_main_menu()
    await message.reply(text, reply_markup=reply_markup)

# --- BUTON TIKLAMALARI VE ALT MENÜLER ---
@app.on_callback_query()
async def menu_callback(client, callback_query: CallbackQuery):
    global GROUP_ID
    data = callback_query.data

    if data == "main_menu":
        await callback_query.answer()
        text, reply_markup = get_main_menu()
        await callback_query.message.edit(text, reply_markup=reply_markup)

    elif data == "menu_deleted":
        await callback_query.answer()
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Temizliği Başlat", callback_data="run_deleted")],
            [InlineKeyboardButton("🔙 Geri", callback_data="main_menu")]
        ])
        await callback_query.message.edit(
            "🗑 **Silinmiş Hesap Temizleme**\n\n"
            "Bu işlem gruptaki silinmiş hesapları (Deleted Account) bulur ve gruptan çıkarır.\n\n"
            "⚠️ İşlemi başlatmak için aşağıdaki butona basın. Sorularınız için @saklanmam kisisine ulasın.",
            reply_markup=buttons
        )

    elif data == "menu_id_kick":
        await callback_query.answer()
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Geri", callback_data="main_menu")]
        ])
        await callback_query.message.edit(
            "📄 **ID / Username ile Gruptan Çıkarma**\n\n"
            "ONUNE cıkarılcak kisilerin ıd ve ya @USARNAME Sİ txt şeklinde hazırlarıyın "
            "ve @saklanmam kisisine ulasın gerekili bilgi verilcektir.\n\n"
            "💡 _Not: Listeniz hazır olduğunda txt dosyasını direkt buraya gönderebilirsiniz._",
            reply_markup=buttons
        )

    elif data == "run_deleted":
        if not GROUP_ID:
            await callback_query.answer("Hata: Bot henüz bir gruba eklenmemiş!", show_alert=True)
            return

        await callback_query.answer("Temizlik işlemi başlatılıyor...", show_alert=False)
        msg = await callback_query.message.edit("🗑 Silinmis hesablar cıkarılıyor, lütfen bekleyin...")
        
        try:
            chat = await client.get_chat(GROUP_ID)
            group_name = chat.title
        except:
            buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Geri", callback_data="main_menu")]])
            return await msg.edit("❌ Bot gruptan çıkarılmış veya yetkisi yok. @saklanmam ile iletişime geçin.", reply_markup=buttons)

        silinen, hata = 0, 0
        async for member in client.get_chat_members(GROUP_ID):
            try:
                if member.user.is_deleted:
                    if member.status in ("administrator", "creator"):
                        continue
                    await client.ban_chat_member(GROUP_ID, member.user.id)
                    await client.unban_chat_member(GROUP_ID, member.user.id)
                    silinen += 1
                    await asyncio.sleep(0.1)
            except:
                hata += 1

        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Ana Menü", callback_data="main_menu")]])
        await msg.edit(
            "✅ **Tarama Tamamlandı!**\n\n"
            f"📌 **Grup:** {group_name}\n"
            f"🗑 **Çıkarılan Silinmiş Hesap:** {silinen}\n"
            f"❌ **Hata:** {hata}\n\n"
            "Destek ve aktivasyon için @saklanmam kisisinse ulasın.",
            reply_markup=buttons
        )

# --- ORTAK TXT İŞLEME FONKSİYONU ---
async def process_txt_file(client, message, file_path):
    global GROUP_ID
    try:
        chat = await client.get_chat(GROUP_ID)
        group_name = chat.title
    except:
        return await message.reply("❌ Bot grupta aktif değil veya çıkarılmış. Lütfen @saklanmam kisisine ulasın.")

    status_msg = await message.reply("⏳ Liste işleniyor ve işlemler yapılıyor...")
    cikarilan, hata, bulunamadi = 0, 0, 0

    try:
        with open(file_path, "r", encoding="utf-8") as file:
            lines = file.readlines()

        await status_msg.edit(f"⏳ Toplam {len(lines)} satır işleniyor...")

        for line in lines:
            user_input = line.strip()
            if not user_input:
                continue

            try:
                if user_input.isdigit() or (user_input.startswith("-") and user_input[1:].isdigit()):
                    user_to_kick = int(user_input)
                else:
                    user_to_kick = user_input if user_input.startswith("@") else f"@{user_input}"

                await client.ban_chat_member(GROUP_ID, user_to_kick)
                await client.unban_chat_member(GROUP_ID, user_to_kick)
                cikarilan += 1
                await asyncio.sleep(0.15)

            except Exception as e:
                error_str = str(e)
                if "USER_ID_INVALID" in error_str or "USERNAME_NOT_OCCUPIED" in error_str:
                    bulunamadi += 1
                else:
                    hata += 1

        await status_msg.reply(
            "📊 **Toplu Çıkarma İşlemi Tamamlandı!**\n\n"
            f"📌 **Hedef Grup:** {group_name}\n"
            f"✅ **Gruptan Çıkarılan:** {cikarilan}\n"
            f"🔍 **Grupta Olmayan:** {bulunamadi}\n"
            f"❌ **Hata/Yetki Yok:** {hata}\n\n"
            "Sorun yaşadıysanız @saklanmam kisisine ulasın."
        )
    except Exception as e:
        await status_msg.edit(f"❌ Dosya okunurken bir hata oluştu: {e}")

# --- TXT DOSYASI YAKALAYICI ---
@app.on_message(filters.private & filters.document)
async def handle_txt_file(client, message):
    global GROUP_ID, LAST_TXT_PATH
    if not GROUP_ID:
        return await message.reply("❌ Bot henüz bir gruba eklenmemiş. Lütfen önce bota grup kurdurun ve @saklanmam kisisine ulasın.")

    if not message.document.file_name.endswith('.txt'):
        return await message.reply("❌ Lütfen sadece `.txt` uzantılı bir dosya gönderin.")

    # Eski geçici dosyayı sil (varsa) ve yenisini kaydet
    if LAST_TXT_PATH and os.path.exists(LAST_TXT_PATH):
        try:
            os.remove(LAST_TXT_PATH)
        except:
            pass

    status_msg = await message.reply("📥 Liste indiriliyor...")
    LAST_TXT_PATH = await message.download()
    await status_msg.delete()
    
    await process_txt_file(client, message, LAST_TXT_PATH)

# --- REFRESH KOMUTU ---
@app.on_message(filters.private & filters.command("refresh"))
async def refresh_last_file(client, message):
    global GROUP_ID, LAST_TXT_PATH
    if not GROUP_ID:
        return await message.reply("❌ Bot henüz bir gruba eklenmemiş.")
    
    if not LAST_TXT_PATH or not os.path.exists(LAST_TXT_PATH):
        return await message.reply("❌ Hafızada işlenecek son bir `.txt` dosyası bulunamadı. Lütfen önce bir dosya gönderin.")

    await message.reply("🔄 Son gönderilen dosya tekrar işleniyor...")
    await process_txt_file(client, message, LAST_TXT_PATH)

# --- GRUP İÇİNDE SİLİNMİŞ HESAP TARAMA KOMUTU ---
@app.on_message(filters.command("deleted") & filters.group)
async def remove_deleted_group(client, message):
    global GROUP_ID
    GROUP_ID = message.chat.id
    
    msg = await message.reply("🗑 Silinmiş hesaplar temizleniyor...")
    silinen, hata = 0, 0

    async for member in client.get_chat_members(message.chat.id):
        try:
            if member.user.is_deleted:
                if member.status in ("administrator", "creator"):
                    continue
                await client.ban_chat_member(message.chat.id, member.user.id)
                await client.unban_chat_member(message.chat.id, member.user.id)
                silinen += 1
                await asyncio.sleep(0.1)
        except:
            hata += 1

    await msg.edit(
        "✅ Temizlik tamamlandı.\n"
        f"🗑 Çıkarılan silinmiş hesap: {silinen}\n"
        f"❌ Hata: {hata}\n"
        "Destek: @saklanmam"
    )

print("Bot çalışıyor...")
app.run()