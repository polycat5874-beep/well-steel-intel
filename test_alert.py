# -*- coding: utf-8 -*-
"""Simulate a Critical Alert + Daily Summary instantly with fake news items
(no waiting for real news, never touches news.db).

  python test_alert.py            run both simulations (dry-run prints to console)
  python test_alert.py --critical critical alert only
  python test_alert.py --daily    daily summary only

With LINE credentials in .env the messages are actually delivered to the
Official Account - use this to verify the channel end-to-end. Without them
everything runs in dry-run and is printed to the console instead.
"""
import argparse
import logging
import sys

from main import load_env, BASE_DIR  # noqa: F401  (also sets sys.path)
from src import notifier, summarizer
from src.matcher import Matcher

FAKE_NEWS = [
    {
        "title": "ด่วน! สมอ. เปิดรับฟังความเห็นแก้ มอก. 24-2559 ตัดเหล็กเส้นจากเตา IF ออกจากมาตรฐาน",
        "url": "https://example.com/tisi-if-amendment",
        "source": "ประชาชาติธุรกิจ",
        "source_name": "ประชาชาติธุรกิจ",
        "published": "Tue, 10 Jun 2026 14:32:00 +0700",
        "published_datetime": "2026-06-10T14:32:00",
        "summary": "สมอ. เตรียมแก้ มอก. 24-2559 ตัดเหล็กเส้นที่ผลิตจากเตาอินดักชั่น (IF) ออกจากมาตรฐานบังคับ. กระทบโรงงานเหล็กเส้นเตา IF ทั่วประเทศ ต้องปรับสายการผลิตหรือเปลี่ยนชนิดเตา.",
    },
    {
        "title": "กรมการค้าต่างประเทศประกาศต่ออายุ AD เหล็กลวดคาร์บอนสูงจากจีนอีก 5 ปี อัตรา 12.26%-36.79%",
        "url": "https://example.com/dft-ad-wire-rod",
        "source": "ฐานเศรษฐกิจ",
        "source_name": "ฐานเศรษฐกิจ",
        "published": "Mon, 09 Jun 2026 18:10:00 +0700",
        "published_datetime": "2026-06-09T18:10:00",
        "summary": "ผลทบทวนมาตรการตอบโต้การทุ่มตลาด Wire Rod คาร์บอนสูงจากจีน คงอากรอีก 5 ปี.",
    },
    {
        "title": "ศุลกากรแหลมฉบังจับเหล็กเส้นลักลอบนำเข้าสำแดงเท็จ มูลค่ากว่า 50 ล้านบาท",
        "url": "https://example.com/customs-bust",
        "source": "กรุงเทพธุรกิจ",
        "source_name": "กรุงเทพธุรกิจ",
        "published": "Mon, 09 Jun 2026 09:05:00 +0700",
        "published_datetime": "2026-06-09T09:05:00",
        "summary": "ตรวจพบตู้คอนเทนเนอร์สำแดงเป็นสินค้าอื่น ภายในเป็นเหล็กเส้นจากจีนไม่มีเครื่องหมาย มอก.",
    },
    {
        "title": "ราคาเศษเหล็กในประเทศทรงตัว ยี่ปั๊วชะลอรับซื้อรอทิศทางตลาด",
        "url": "https://example.com/scrap-price",
        "source": "ผู้จัดการออนไลน์",
        "source_name": "ผู้จัดการออนไลน์",
        "published": "Tue, 10 Jun 2026 11:20:00 +0700",
        "published_datetime": "2026-06-10T11:20:00",
        "summary": "ตลาดเศษเหล็กเงียบ ผู้ค้ารอดูราคาบิลเล็ตนำเข้า.",
    },
]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="steel-intel alert simulator")
    parser.add_argument("--critical", action="store_true")
    parser.add_argument("--daily", action="store_true")
    args = parser.parse_args()
    run_all = not (args.critical or args.daily)

    load_env()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    matcher = Matcher()

    analyzed = []
    print("\n--- Matcher analysis of fake items ---")
    for item in FAKE_NEWS:
        analysis = matcher.analyze(item)
        merged = {**item, **analysis}
        analyzed.append(merged)
        print(f"[{analysis['level']:6}] score={analysis['score']:2} "
              f"critical={len(analysis['critical_hits'])} "
              f"watchlist={len(analysis['watchlist_hits'])} | {item['title'][:60]}")

    if run_all or args.critical:
        print("\n--- Simulated CRITICAL ALERT ---")
        top = max(analyzed, key=lambda x: x["score"])
        notifier.send(summarizer.build_critical_alert(top, top))

    if run_all or args.daily:
        print("\n--- Simulated DAILY SUMMARY ---")
        msg = summarizer.build_daily_summary(
            analyzed, matcher.cfg["watchlist"], "ทดสอบระบบ"
        )
        notifier.send(msg)

    print(f"\nDone. Active channel = {notifier.active_channel()} "
          f"(dry-run prints to console; line delivers for real).")


if __name__ == "__main__":
    main()
