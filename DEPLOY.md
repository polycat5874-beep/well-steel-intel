# steel-intel — คู่มือ Deploy 24/7 ฟรี (GitHub Actions + Supabase Postgres)

> แนวทางที่ผู้กองเลือก: **B — GitHub Actions cron + Remote DB (Supabase Postgres)**
> ต้นทุน **0 บาท ไม่ผูกบัตร** แลกกับ cron ดีเลย์ไม่กี่นาที (รับได้สำหรับงานเฝ้าข่าว)
>
> โค้ดพร้อม deploy แล้ว — ผู้กองทำตาม 7 ขั้นล่างนี้ (ผมทำส่วนโค้ดให้หมดแล้ว เหลือขั้นที่ต้องใช้บัญชี GitHub/Supabase/LINE ของผู้กองเอง)

---

## 0) ทำไมต้องทำแบบนี้ (อ่านก่อน 30 วินาที)
- GitHub Actions รันงานบน "เครื่องชั่วคราว" — จบ job แล้ว **ดิสก์ถูกลบทิ้งทุกครั้ง** → ไฟล์ `news.db` อยู่ไม่ได้
- เพราะงั้นต้องย้าย DB ไปไว้ "ข้างนอก" (Supabase) ให้ทุกครั้งที่ cron ยิงมาต่อตัวเดียวกัน → ระบบ dedup/baseline ถึงจะจำข่าวเก่าได้ ไม่ flood ซ้ำ
- โค้ดผมแก้ให้ `storage.py` สลับเป็น Postgres อัตโนมัติ **เมื่อมี `DATABASE_URL`** (ถ้าไม่มี = ใช้ SQLite เหมือนเดิมบนเครื่อง) — ของเดิมไม่พัง

---

## ⚠️ จุดต้องตัดสินใจก่อนเริ่ม: repo เป็น Public หรือ Private?

**สำคัญ:** steel-intel ต้องเป็น **repo ใหม่แยกของตัวเอง** — **ห้ามเอา repo `morman-team` ขึ้น GitHub** (มีข้อมูลลับทีมตำรวจ + ทะเบียนผู้ติดต่อ)

เรื่องโควต้านาที GitHub Actions (แผนฟรี):

| | Public repo | Private repo |
|---|---|---|
| โควต้า Actions | **ไม่จำกัด** | 2,000 นาที/เดือน |
| งานเราใช้จริง (เช็คทุก 15 นาที) | สบาย | **~4,400 นาที/เดือน → เกินโควต้า** |

➡️ **ผมแนะนำ Public repo** — เพราะเช็คทุก 15 นาทีกินเกินโควต้า private แน่นอน
- repo นี้มีแค่ "โค้ด + คำค้นข่าวเหล็ก" **ไม่มีความลับ** (credential อยู่ใน GitHub Secrets ไม่ commit, `.env`/`news.db`/`logs` ถูก gitignore แล้ว)
- ถ้าผู้กองยืนยันจะเอา **Private** → ต้องลดความถี่ realtime เป็นทุก ~45 นาที (ผมแก้ cron ให้) เพื่อให้พอ 2,000 นาที

> ⛔ **ก่อนทำ public ต้อง "Reissue" LINE Channel Secret ก่อน** (ขั้นที่ 1) เพราะ secret เก่าเคยพิมพ์ในแชท

---

## 1) Reissue LINE Channel Secret (กันของเก่ารั่ว)
1. ไป https://developers.line.biz/console/ → เลือก Provider → channel "Well Steel Intel"
2. แท็บ **Basic settings** → หา **Channel secret** → กด **Issue** (ออกตัวใหม่)
3. คัดลอกค่าใหม่ไว้ (เดี๋ยวเอาไปใส่ GitHub Secret ขั้นที่ 4) — **อย่าพิมพ์ลงแชท/อย่า commit**
> Channel ID เท่าเดิม เปลี่ยนแค่ secret | ระบบ re-mint token เองจาก ID+secret ใหม่ได้ทันที

---

## 2) สร้าง Supabase Postgres (Remote DB)
1. ไป https://supabase.com → ใช้บัญชีเดิม (ที่ผูกกับ wellsteel-erp/workpass) ก็ได้
2. **New project** → ตั้งชื่อ เช่น `steel-intel` → ตั้ง **Database Password** (จำไว้) → เลือก region ใกล้ไทย (Singapore)
3. รอ provision ~2 นาที → กดปุ่ม **Connect** (บนสุด) → เลือกแท็บ **Session pooler**
4. คัดลอก connection string หน้าตาแบบนี้ (ใช้ **Session pooler** เพราะรองรับ IPv4 จาก GitHub):
   ```
   postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
   ```
5. แทน `<password>` ด้วยรหัสที่ตั้งไว้ข้อ 2 → **นี่คือค่า `DATABASE_URL`**

---

## 3) ทดสอบเชื่อม DB จากเครื่องก่อน (กันพลาดก่อนขึ้น cloud)
บนเครื่องผู้กอง ในโฟลเดอร์ `steel-intel`:
```powershell
pip install -r requirements.txt            # ติดตั้ง psycopg เพิ่ม
$env:DATABASE_URL = "postgresql://postgres.<ref>:<pass>@...:5432/postgres"
python migrate.py                          # ต้องขึ้น backend: PostgreSQL + schema OK
python main.py --once                      # seed baseline เงียบ (ไม่ยิงข่าว flood)
```
- `migrate.py` ขึ้น `schema OK` = เชื่อม Supabase ติด สร้างตารางแล้ว
- `--once` รอบแรกจะ "ตั้งต้นเงียบ" (mark ข่าวที่มีว่ารู้แล้ว) → cron รอบถัดไปบน cloud ถึงจะแจ้งเฉพาะข่าวใหม่จริง
> เสร็จแล้วล้างตัวแปร: `Remove-Item Env:DATABASE_URL` (กลับไปใช้ SQLite local ตามเดิม)

---

## 4) สร้าง GitHub repo ใหม่ + ใส่ Secrets
**สร้าง repo แยก (เฉพาะเนื้อ steel-intel):**
```powershell
# ทำสำเนา steel-intel ออกมานอก repo morman-team ก่อน (กันพันกับ git เดิม)
Copy-Item -Recurse "D:\claude\claude projec\morman-team\Well Projec\steel-intel" "D:\steel-intel-deploy"
cd D:\steel-intel-deploy
Remove-Item -Recurse -Force .env, news.db, logs, __pycache__, src\__pycache__ -ErrorAction SilentlyContinue
git init; git add .; git commit -m "steel-intel: initial deploy"
```
จากนั้นสร้าง repo บน GitHub (Public แนะนำ) แล้ว push — ใช้ `gh` ก็ได้:
```powershell
gh repo create well-steel-intel --public --source=. --push
```
> ✅ `.gitignore` กัน `.env`/`news.db`/`logs` ไว้แล้ว — รหัสจะไม่หลุดขึ้น repo

**ใส่ Secrets:** ไปที่ repo → **Settings → Secrets and variables → Actions → New repository secret** ใส่ทีละตัว:

| ชื่อ Secret | ค่า |
|---|---|
| `DATABASE_URL` | connection string Supabase (ขั้น 2) |
| `LINE_CHANNEL_ID` | `2010359675` |
| `LINE_CHANNEL_SECRET` | secret **ใหม่** (ขั้น 1) |
| `ANTHROPIC_API_KEY` | (ออปชัน — ใส่ถ้าต้องการสรุป AI เชิงลึก ไม่ใส่ก็ได้) |

> ไม่ต้องใส่ `LINE_USER_ID` — เว้นว่าง = โหมด broadcast (ส่งทุกคนที่แอด OA) ตามที่ใช้อยู่

---

## 5) เปิด GitHub Actions + ทดสอบยิงมือ
1. repo → แท็บ **Actions** → ถ้าถามให้กด **"I understand... enable workflows"**
2. เลือก workflow **"steel-intel realtime"** → กด **Run workflow** (จาก `workflow_dispatch`)
3. ดู log ใน job → ต้องเห็น `LINE send mode: broadcast` หรือ `no critical news pending` (ปกติ)
4. ลอง **"steel-intel daily summary"** → Run workflow → เช็คว่าข้อความสรุปเข้า LINE OA `@959laabn`

---

## 6) ปล่อยรันอัตโนมัติ
- เปิด Actions แล้ว cron ทำงานเองตามนี้ (เวลาไทย):
  - **realtime ทุก ~15 นาที** → เจอข่าว critical = ยิงด่วน
  - **สรุป 07:00 / 12:00 / 18:00** → digest รวบ
- **ไม่ต้องเปิดเครื่องผู้กองอีกต่อไป** — GitHub รันบน cloud ให้ 24/7

---

## 7) ข้อควรรู้ / กับดักที่ต้องระวัง
- **cron รันบน default branch เท่านั้น** — push โค้ดอยู่ที่ `main`/`master`
- **GitHub ปิด scheduled workflow อัตโนมัติถ้า repo เงียบ 60 วัน** (ไม่มี commit) — แค่ push อะไรเล็กน้อยทุก ~1-2 เดือน หรือกด Run workflow มือก็รีเซ็ตตัวนับ
- **cron ดีเลย์ได้** ช่วง GitHub โหลดเยอะ อาจช้า 5-20 นาที (นี่คือข้อแลกของแผนฟรีที่ตกลงกันแล้ว)
- **เวลาเป็น UTC ใน cron** แต่ผม map ให้ตรงเวลาไทยแล้ว (07/12/18 ไทย = 00/05/11 UTC) + ตั้ง `TZ=Asia/Bangkok` ใน workflow ให้ label เช้า/เที่ยง/เย็นถูก
- **อยากแก้ความถี่** → แก้บรรทัด `cron:` ใน `.github/workflows/realtime.yml`
- **อยากปิดชั่วคราว** → แท็บ Actions → workflow → `...` → Disable

---

## สรุปไฟล์ที่เพิ่ม/แก้ในรอบนี้
| ไฟล์ | ทำอะไร |
|---|---|
| `src/storage.py` | เพิ่มแบ็กเอนด์ Postgres (สลับอัตโนมัติเมื่อมี `DATABASE_URL`), ของเดิม SQLite ไม่พัง |
| `requirements.txt` | เพิ่ม `psycopg[binary]` |
| `.env.example` | เพิ่มตัวอย่าง `DATABASE_URL` |
| `migrate.py` | สคริปต์ทดสอบเชื่อม DB + สร้าง schema + เช็คสถานะ |
| `.github/workflows/realtime.yml` | cron เช็คข่าวทุก 15 นาที |
| `.github/workflows/summary.yml` | cron สรุป 07/12/18 น. |
