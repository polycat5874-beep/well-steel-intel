# 🛰️ steel-intel — ระบบเฝ้าข่าวอุตสาหกรรมเหล็ก + แจ้งเตือนผู้บริหาร

เฝ้าข่าว 9 หัวข้อจากหลายแหล่ง → แจ้งเตือนด่วนผ่าน **LINE** เมื่อเจอข่าวสำคัญ (ทุก 15 นาที) + สรุปประจำวัน 3 รอบ (07:00 / 12:00 / 18:00) พร้อม **Impact Scoring เทียบโปรไฟล์บริษัท** และ **Watchlist นับถอยหลัง** (AD เหล็กลวด พ.ค. 2026 / แก้ มอก. ตัดเหล็ก IF / circumvention HRC)

> ช่องทางแจ้งเตือนหลัก = **LINE Messaging API (LINE Official Account)** | Telegram = fallback ออปชัน | notifier เป็น module แยก เพิ่ม Slack/อีเมลได้

## ติดตั้งครั้งแรก
```
cd steel-intel
pip install -r requirements.txt
copy .env.example .env
```

### ตั้งค่า LINE (ช่องทางหลัก) — ใส่ใน `.env`
1. เข้า **developers.line.biz/console** → login ด้วยบัญชี LINE → **Create** Provider (เช่น `Well Steel Intel`)
2. ใน Provider → **Create a new channel** → เลือก **Messaging API** (ระบบสร้าง LINE Official Account ให้)
3. แท็บ **Messaging API** → **Channel access token (long-lived)** → กด **Issue** → คัดลอกใส่ `LINE_CHANNEL_ACCESS_TOKEN`
4. แท็บ **Basic settings** → ล่างสุด **Your user ID** (`uXXXX...`) → ใส่ `LINE_USER_ID`
5. ⚠️ **เพิ่มเพื่อน OA นั้นก่อน** (สแกน QR ในแท็บ Messaging API) ไม่งั้น push ไม่ถึง
6. (แนะนำ) ปิด auto-reply ที่ LINE Official Account Manager

> 💡 LINE OA ฟรี ส่ง push ~500 ข้อความ/เดือน — ระบบรวบหลายข่าวใน 1 push เพื่อประหยัดโควต้า เกินค่อยอัปเกรดแพ็กเกจ

### ออปชัน
- **Telegram fallback:** ถ้าไม่กรอก LINE แต่กรอก `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` ระบบจะใช้ Telegram แทนอัตโนมัติ
- **`ANTHROPIC_API_KEY`:** เปิดบทวิเคราะห์ AI ท้ายสรุปประจำวัน — ไม่มีก็ทำงานครบ (Impact Scoring เป็น rule-based อยู่แล้ว)
- ไม่กรอกช่องทางใดเลย = **DRY-RUN** (พิมพ์ข้อความลง console ใช้ทดสอบ)

## ใช้งาน
| คำสั่ง | ทำอะไร |
|--------|--------|
| `start.bat` หรือ `python main.py` | รันระบบจริง (realtime 15 นาที + สรุป 3 รอบ/วัน) — เปิดทิ้งไว้ |
| `python test_alert.py` | **ทดสอบทันที** จำลอง Critical Alert + Daily Summary ด้วยข่าวปลอม (ไม่มี token = พิมพ์ลงจอ) |
| `python main.py --once` | ดึงข่าวจริง 1 รอบ + แจ้งเตือนถ้าเจอข่าวสำคัญ แล้วจบ |
| `python main.py --summary` | บังคับส่งสรุปประจำวันเดี๋ยวนี้ |

## ปรับแต่ง (ไม่ต้องแก้โค้ด)
- `config/keywords.json` — Critical Keywords / หัวข้อ tracking 9 ข้อ / คะแนน impact เทียบโปรไฟล์บริษัท / Watchlist / เกณฑ์ระดับ 🔴🟠🟡
- `config/sources.json` — คำค้น Google News, RSS สำนักข่าว, URL หน้าข่าวเว็บราชการ (เว็บเปลี่ยนโครง = แก้ URL ที่นี่)

## หมายเหตุการทำงาน
- ข้อมูลสะสมใน `news.db` (SQLite) — กันข่าวซ้ำด้วย hash(URL+หัวข้อ) ข่าวเดิมไม่แจ้งซ้ำ
- เว็บล่ม/feed พัง = ข้ามแหล่งนั้นพร้อม log เตือน ระบบไม่หยุด (Google News site: query เป็น backstop ของเว็บราชการ)
- log อยู่ที่ `logs/steel_intel.log`
- ช่วงแรกถ้าแจ้งเตือนถี่เกินไป: ลดคำใน `critical_keywords` หรือเพิ่ม `score_red`/`score_orange` ใน settings
