/* คลังข่าวอุตสาหกรรมเหล็ก — ตัวหน้าเว็บ
 *
 * ทุกอย่างในไฟล์นี้ทำงานในเบราว์เซอร์ล้วน จากข้อมูลก้อนเดียวที่ฝังอยู่ในบล็อก
 * ชนิด application/json ที่มีไอดี "d" (ดู src/archive.py)
 * ไม่มีการเรนเดอร์รายการข่าวล่วงหน้าเป็น HTML อีกต่อไป (เคยทำแล้วได้ไฟล์
 * 1.14 MB ที่ 62% เป็นข้อความซ้ำกับ JSON ก้อนเดียวกัน)
 *
 * ข้อบังคับที่ต้องรักษาไว้เมื่อแก้ไฟล์นี้
 *   - ห้ามเรียกไฟล์/เน็ตภายนอกทุกชนิด หน้าเว็บต้องเปิดจาก file:// แบบถอดสายเน็ต
 *     แล้วใช้งานได้ครบ
 *   - พาดหัวข่าวคือข้อความจากภายนอก ต้อง esc() ก่อนเข้า innerHTML เสมอ
 *   - ลิงก์ต้องผ่าน safeUrl() (รับเฉพาะ http:// และ https://) ฝั่ง Python ก็กรอง
 *     ซ้ำอีกชั้น
 *   - ห้ามใช้ชื่อฟิลด์ภายในของฐานข้อมูล (รายชื่ออยู่ที่ FORBIDDEN_TOKENS ใน
 *     src/archive.py) เป็นชื่อคลาส ตัวแปร หรือไอดีในไฟล์นี้ — ด่านตรวจถือว่าคำ
 *     พวกนั้นโผล่ในไฟล์ที่เผยแพร่ = มีแถวดิบหลุดออกมา ให้ใช้ rank / sig / mark /
 *     seen แทน (ยามตรวจไฟล์ web/ ทุกไฟล์ ไม่เว้นแม้แต่คอมเมนต์)
 *   - อีโมจิระดับความสำคัญเขียนเป็น \uXXXX เท่านั้น หน้าเว็บที่ปิดการแสดงระดับ
 *     (archive_include_level=false) ต้องไม่มีอักขระพวกนั้นอยู่ในไฟล์เลย
 */
(function () {
  "use strict";

  var dataEl = document.getElementById("d");
  var listEl = document.getElementById("list");
  var D = null;
  try {
    D = JSON.parse((dataEl && (dataEl.textContent || dataEl.innerText)) || "null");
  } catch (err) {
    D = null;
  }
  if (!D || !D.rows) {
    if (listEl) { listEl.textContent = "อ่านข้อมูลในหน้านี้ไม่สำเร็จ"; }
    return;
  }

  /* ---------------------------------------------------------------- ค่าคงที่ */

  /* เขียนเป็น \uXXXX ตั้งใจ: หน้าที่ปิดการแสดงระดับต้องไม่มีอักขระอีโมจิ
     พวกนี้อยู่ในไฟล์เลยแม้แต่ตัวเดียว (ด่านตรวจ H16) */
  var DOT = { R: "\ud83d\udd34", O: "\ud83d\udfe0",
              Y: "\ud83d\udfe1", G: "\u26aa" };
  var LVKEYS = ["R", "O", "Y", "G"];
  var LVNAME = { R: "แดง", O: "ส้ม", Y: "เหลือง", G: "เทา" };
  var MONTH = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"];
  var MSHORT = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."];
  var PAGE_STEP = 100;
  var STALE_MS = 26 * 60 * 60 * 1000;   /* ยามเฝ้าตัวเอง: เกิน 26 ชม. = ผิดปกติ */
  var SHOW_LEVEL = !!D.lv;

  /* ------------------------------------------------------------- ตัวช่วยสั้น */

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return c === "&" ? "&amp;" : c === "<" ? "&lt;" : c === ">" ? "&gt;"
        : c === '"' ? "&quot;" : "&#39;";
    });
  }

  function nf(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }

  function show(el, yes) { if (el) { el.className = el.className.replace(/\s*\bhide\b/g, "") + (yes ? "" : " hide"); } }

  /* เวลาเก็บเป็น "2026-08-31T15:50" เสมอ (Asia/Bangkok) จึงตัดสตริงตรงๆ ได้
     ปลอดภัยกว่าโยนเข้า Date() ที่จะถูกตีความเป็นเวลาท้องถิ่นของเครื่องผู้อ่าน */
  function dmy(stamp) {
    var d = String(stamp || "").slice(0, 10).split("-");
    return d.length === 3 && d[0] ? d[2] + "/" + d[1] + "/" + d[0] : "";
  }

  function hhmm(stamp) { return String(stamp || "").slice(11, 16); }

  function when(stamp) {
    var day = dmy(stamp);
    if (!day) { return "ไม่ทราบวันที่"; }
    var t = hhmm(stamp);
    return t ? day + " " + t : day;
  }

  function thaiDay(day) {
    var p = String(day || "").split("-");
    if (p.length !== 3) { return "ไม่ทราบวันที่"; }
    return parseInt(p[2], 10) + " " + (MONTH[parseInt(p[1], 10) - 1] || "") + " " + p[0];
  }

  function safeUrl(u) {
    var t = String(u || "").trim();
    var low = t.toLowerCase();
    return (low.indexOf("http://") === 0 || low.indexOf("https://") === 0) ? t : "";
  }

  /* -------------------------------------------------- แถว + ตัวดัชนีค้นหา */

  var ROWS = [];
  var i, a;
  for (i = 0; i < D.rows.length; i++) {
    a = D.rows[i];
    ROWS.push({
      n: i,
      id: a[0],
      t: String(a[1] || ""),
      u: (a[2] >= 0 ? (D.pre[a[2]] || "") : "") + String(a[3] || ""),
      s: D.src[a[4]] || "",
      d: String(a[5] || ""),
      df: a[6] ? 1 : 0,
      sm: String(a[7] || ""),
      lv: String(a[8] || ""),
      tp: a[9] || [],
      g: a[10]
    });
  }

  /* จำนวนสำนักที่รายงานเรื่องเดียวกัน (g ร่วมกัน) */
  var GRP = {};
  for (i = 0; i < ROWS.length; i++) {
    if (ROWS[i].g >= 0) { GRP[ROWS[i].g] = (GRP[ROWS[i].g] || 0) + 1; }
  }

  /* norm(): พิมพ์เล็ก + เลขไทย ๐-๙ เป็น 0-9 — ทั้งสองอย่างไม่เปลี่ยนความยาว
     สตริง ตำแหน่งที่หาเจอจึงชี้กลับไปยังข้อความต้นฉบับได้ตรงตัว (ใช้ทำไฮไลต์) */
  function norm(s) {
    return String(s == null ? "" : s).toLowerCase()
      .replace(/[๐-๙]/g, function (c) {
        return String.fromCharCode(c.charCodeAt(0) - 0x0e50 + 48);
      });
  }

  var GAPS = /[\s\u200b-\u200d\u00a0\u0021-\u002f\u003a-\u0040\u005b-\u0060\u007b-\u007e\u2010-\u2027\u3000-\u303f]+/g;

  /* squash(): ตัดช่องว่างและวรรคตอนทิ้งหมด — ชั้นนี้แก้เคสจริงที่ cluster.py
     เจอมาแล้ว คือ "ซิน เคอ หยวน" กับ "ซินเคอหยวน" ที่เป็นชื่อเดียวกัน */
  function squash(s) { return String(s).replace(GAPS, ""); }

  /* สองชุดนี้อยู่ในหน่วยความจำเท่านั้น ไม่ถูกเขียนลงไฟล์ (ไม่งั้นไฟล์บวมเท่าตัว) */
  var HAY = [], HAYS = [];
  for (i = 0; i < ROWS.length; i++) {
    var names = [];
    for (var k = 0; k < ROWS[i].tp.length; k++) { names.push(D.top[ROWS[i].tp[k]] || ""); }
    var blob = norm(ROWS[i].t + " " + ROWS[i].sm + " " + ROWS[i].s + " " + names.join(" "));
    HAY.push(blob);
    HAYS.push(squash(blob));
  }

  function terms(q) {
    var out = [], parts = norm(q).split(/\s+/);
    for (var j = 0; j < parts.length; j++) {
      if (parts[j]) { out.push({ q: parts[j], s: squash(parts[j]) }); }
    }
    return out;
  }

  function hits(idx, list) {
    for (var j = 0; j < list.length; j++) {
      var ok = HAY[idx].indexOf(list[j].q) >= 0;
      if (!ok && list[j].s) { ok = HAYS[idx].indexOf(list[j].s) >= 0; }
      if (!ok) { return false; }
    }
    return true;
  }

  /* ไฮไลต์: escape ก่อนเสมอ แล้วค่อยแทรก <mark> — ห้ามยัดพาดหัวดิบเข้า innerHTML */
  function hi(text, list) {
    var s = String(text || "");
    if (!list.length) { return esc(s); }
    var n = norm(s), spans = [], j, at;
    for (j = 0; j < list.length; j++) {
      if (!list[j].q) { continue; }
      at = n.indexOf(list[j].q);
      while (at >= 0) {
        spans.push([at, at + list[j].q.length]);
        at = n.indexOf(list[j].q, at + list[j].q.length);
      }
    }
    if (!spans.length) { return esc(s); }
    spans.sort(function (x, y) { return x[0] - y[0]; });
    var out = "", pos = 0;
    for (j = 0; j < spans.length; j++) {
      if (spans[j][0] < pos) { continue; }
      out += esc(s.slice(pos, spans[j][0])) + "<mark>"
        + esc(s.slice(spans[j][0], spans[j][1])) + "</mark>";
      pos = spans[j][1];
    }
    return out + esc(s.slice(pos));
  }

  /* ------------------------------------------------------------------ สถานะ */

  var ST = { q: "", lv: {}, tp: {}, src: -1, rg: "all", from: "", to: "", v: "list", m: "" };
  var SEL = {};
  var LIMIT = PAGE_STEP;
  var RESULT = [];
  var TIMER = null;

  function todayISO() {
    var now = new Date();
    function p(v) { return (v < 10 ? "0" : "") + v; }
    return now.getFullYear() + "-" + p(now.getMonth() + 1) + "-" + p(now.getDate());
  }

  function shiftISO(days) {
    var now = new Date(Date.now() - days * 86400000);
    function p(v) { return (v < 10 ? "0" : "") + v; }
    return now.getFullYear() + "-" + p(now.getMonth() + 1) + "-" + p(now.getDate());
  }

  function rangeBounds() {
    if (ST.rg === "all") { return null; }
    if (ST.rg === "custom") {
      if (!ST.from && !ST.to) { return null; }
      return { a: ST.from || "0000-00-00", b: ST.to || "9999-99-99" };
    }
    return { a: shiftISO(parseInt(ST.rg, 10)), b: todayISO() };
  }

  function anyOn(obj) {
    for (var k in obj) { if (obj[k]) { return true; } }
    return false;
  }

  /* กรองทุกอย่าง "ยกเว้นเดือน" เพื่อให้กราฟยังแสดงเดือนอื่นให้กดสลับได้ */
  function baseSet() {
    var list = terms(ST.q);
    var bounds = rangeBounds();
    var useLv = SHOW_LEVEL && anyOn(ST.lv);
    var useTp = anyOn(ST.tp);
    var out = [];
    for (var j = 0; j < ROWS.length; j++) {
      var r = ROWS[j];
      if (useLv && !ST.lv[r.lv]) { continue; }
      if (ST.src >= 0 && D.src[ST.src] !== r.s) { continue; }
      if (bounds) {
        var day = r.d.slice(0, 10);
        if (!day || day < bounds.a || day > bounds.b) { continue; }
      }
      if (useTp) {
        var got = false;
        for (var m = 0; m < r.tp.length; m++) { if (ST.tp[r.tp[m]]) { got = true; break; } }
        if (!got) { continue; }
      }
      if (list.length && !hits(j, list)) { continue; }
      out.push(r);
    }
    return out;
  }

  /* --------------------------------------------------------------- แถบหัว */

  function header() {
    var stamp = $("stamp"), stamps = [], j;
    for (j = 0; j < ROWS.length; j++) { if (ROWS[j].d) { stamps.push(ROWS[j].d); } }
    stamps.sort();
    var bits = ["อัปเดตล่าสุด " + when(D.gen) + " น."];
    if (stamps.length) {
      bits.push("ครอบคลุม " + dmy(stamps[0]) + " – " + dmy(stamps[stamps.length - 1]));
    }
    bits.push(nf(ROWS.length) + " ชิ้น");
    var pg = D.pg || {};
    if (pg.t && pg.t > ROWS.length) {
      bits.push("จากทั้งหมด " + nf(pg.t) + " ชิ้น (ที่เหลืออยู่ครบในหน้าไตรมาส)");
    }
    if (pg.lb && pg.k === "quarter") { bits.unshift(pg.lb); }
    if (stamp) { stamp.textContent = bits.join(" · "); }

    var late = (typeof D.genms === "number") && (Date.now() - D.genms > STALE_MS);
    var top = $("top"), alarm = $("alarm");
    if (late) {
      if (top) { top.className = "top stale"; }
      if (alarm) {
        alarm.textContent = "⚠️ คลังยังไม่อัปเดตเกิน 26 ชั่วโมง — "
          + "ระบบสร้างหน้าอาจขัดข้อง";
        show(alarm, true);
      }
    }
  }

  /* ------------------------------------------------------------ แถบชิปกรอง */

  function chip(label, attr) {
    return '<button type="button" class="" ' + attr + ">" + esc(label) + "</button>";
  }

  /* ชิปถูกสร้าง "ครั้งเดียว" ตอนเปิดหน้า แล้วหลังจากนั้นแตะแค่คลาส on เท่านั้น
     ถ้าสร้างใหม่ทุกครั้งที่กรอง แถบที่เลื่อนแนวนอนบนมือถือจะเด้งกลับไปซ้ายสุด
     ทุกตัวอักษรที่พิมพ์ และปุ่มที่ผู้ใช้กำลังกดค้างอยู่จะกลายเป็นปุ่มที่หลุด
     จาก DOM ไปแล้ว */
  function buildFilters() {
    var html, j;

    if (SHOW_LEVEL) {
      html = chip("ทั้งหมด", 'data-lv="*"');
      for (j = 0; j < LVKEYS.length; j++) {
        html += chip(DOT[LVKEYS[j]], 'data-lv="' + LVKEYS[j]
          + '" title="' + LVNAME[LVKEYS[j]] + '"');
      }
      $("lvrow").innerHTML = '<span class="tag">ระดับ</span>' + html;
      show($("lvrow"), true);
    }

    var ranges = [["7", "7 วัน"], ["30", "30 วัน"], ["90", "90 วัน"],
      ["all", "ทั้งหมด"], ["custom", "กำหนดเอง"]];
    html = "";
    for (j = 0; j < ranges.length; j++) {
      html += chip(ranges[j][1], 'data-rg="' + ranges[j][0] + '"');
    }
    $("rgrow").innerHTML = '<span class="tag">ช่วงเวลา</span>' + html;

    html = chip("ทั้งหมด", 'data-tp="*"');
    for (j = 0; j < D.top.length; j++) {
      html += chip(D.top[j], 'data-tp="' + j + '"');
    }
    $("tprow").innerHTML = '<span class="tag">หัวข้อ</span>' + html;

    var counts = {};
    for (j = 0; j < ROWS.length; j++) { counts[ROWS[j].s] = (counts[ROWS[j].s] || 0) + 1; }
    var order = [];
    for (j = 0; j < D.src.length; j++) { order.push(j); }
    order.sort(function (x, y) {
      var dx = (counts[D.src[y]] || 0) - (counts[D.src[x]] || 0);
      return dx || (D.src[x] < D.src[y] ? -1 : 1);
    });
    html = '<option value="-1">ทุกแหล่ง (' + nf(D.src.length) + ")</option>";
    for (j = 0; j < order.length; j++) {
      var nm = D.src[order[j]];
      if (!counts[nm]) { continue; }
      html += '<option value="' + order[j] + '">' + esc(nm) + " (" + nf(counts[nm]) + ")</option>";
    }
    $("srcsel").innerHTML = html;
  }

  function paint(nodes, attr, isOn) {
    for (var j = 0; j < nodes.length; j++) {
      nodes[j].className = isOn(nodes[j].getAttribute(attr)) ? "on" : "";
    }
  }

  function syncFilters() {
    if (SHOW_LEVEL) {
      paint($("lvrow").getElementsByTagName("button"), "data-lv", function (key) {
        return key === "*" ? !anyOn(ST.lv) : !!ST.lv[key];
      });
    }
    paint($("rgrow").getElementsByTagName("button"), "data-rg", function (key) {
      return ST.rg === key;
    });
    show($("cusrow"), ST.rg === "custom");
    paint($("tprow").getElementsByTagName("button"), "data-tp", function (key) {
      return key === "*" ? !anyOn(ST.tp) : !!ST.tp[parseInt(key, 10)];
    });
    if ($("srcsel").value !== String(ST.src)) { $("srcsel").value = String(ST.src); }
    $("vlist").className = ST.v === "list" ? "on" : "";
    $("vtl").className = ST.v === "tl" ? "on" : "";
  }

  /* ------------------------------------------------------- กราฟแท่งรายเดือน */

  function chart(base) {
    var buckets = {}, order = [], j, key;
    for (j = 0; j < base.length; j++) {
      key = base[j].d.slice(0, 7);
      if (!key) { continue; }
      if (!buckets[key]) { buckets[key] = 0; order.push(key); }
      buckets[key]++;
    }
    order.sort();
    var box = $("chart");
    if (!order.length) {
      box.innerHTML = "";
      show(box, false);
      show($("chartnote"), false);
      return;
    }
    show(box, true);
    show($("chartnote"), true);
    var top = 1;
    for (j = 0; j < order.length; j++) { if (buckets[order[j]] > top) { top = buckets[order[j]]; } }
    var html = "";
    for (j = 0; j < order.length; j++) {
      key = order[j];
      var mth = parseInt(key.slice(5, 7), 10) - 1;
      var pct = Math.max(3, Math.round(buckets[key] * 100 / top));
      html += '<button type="button" class="col' + (ST.m === key ? " on" : "")
        + '" data-m="' + key + '" title="' + esc((MONTH[mth] || "") + " " + key.slice(0, 4)
          + " · " + nf(buckets[key]) + " ชิ้น") + '">'
        + '<span class="fill" style="height:' + pct + '%"></span>'
        + '<span class="lab">' + esc(MSHORT[mth] || "") + "</span>"
        + '<span class="lab">' + esc(key.slice(2, 4)) + "</span></button>";
    }
    box.innerHTML = html;
    /* เลื่อนไปสุดขวาเมื่อผู้ใช้ยังไม่ได้เลือกเดือนเอง: คลังนี้มีข่าวเก่าปี 2015
       ปนอยู่ไม่กี่ชิ้น ถ้าเปิดมาแล้วค้างที่ซ้ายสุดจะเห็นแต่แท่งจิ๋วของปีเก่า
       ส่วนเดือนที่มีข่าวจริงหลายร้อยชิ้นอยู่นอกจอ */
    if (!ST.m) {
      try { box.scrollLeft = box.scrollWidth; } catch (err) { /* ไม่เป็นไร */ }
    }
  }

  /* --------------------------------------------------------------- การ์ดข่าว */

  function card(r, list) {
    var meta = [];
    if (r.df) {
      meta.push('<span class="when">🔎 ' + esc(when(r.d)) + "</span>"
        + '<span class="seen" title="ข่าวชิ้นนี้ไม่มีวันที่ตีพิมพ์ ระบบจึงแสดงเวลาที่'
        + 'พบข่าวแทน">เวลาที่ระบบพบข่าว</span>');
    } else {
      meta.push('<span class="when" title="เวลาที่ตีพิมพ์">🗓️ '
        + esc(when(r.d)) + "</span>");
    }
    if (r.s) { meta.push("<span>🌐 " + esc(r.s) + "</span>"); }
    if (SHOW_LEVEL && DOT[r.lv]) {
      meta.push('<span class="rank" title="ระดับ' + LVNAME[r.lv] + '">' + DOT[r.lv] + "</span>");
    }

    var url = safeUrl(r.u);
    var head = url
      ? '<a class="hl" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">'
        + hi(r.t, list) + "</a>"
      : '<span class="hl">' + hi(r.t, list) + "</span>";

    var tags = "";
    for (var j = 0; j < r.tp.length; j++) {
      tags += '<button type="button" class="tchip' + (ST.tp[r.tp[j]] ? " on" : "")
        + '" data-tp="' + r.tp[j] + '">' + esc(D.top[r.tp[j]] || "") + "</button>";
    }

    var dup = (r.g >= 0 && GRP[r.g] > 1)
      ? '<div class="dup">📰 ซ้ำ ' + nf(GRP[r.g]) + " สำนัก</div>" : "";

    return '<article class="card' + (SEL[r.id] ? " picked" : "") + '">'
      + '<input type="checkbox" class="pick" data-id="' + esc(r.id) + '"'
      + (SEL[r.id] ? " checked" : "") + ' aria-label="เลือกข่าวนี้">'
      + '<div class="meta">' + meta.join("") + "</div>"
      + head
      + (r.sm ? '<p class="sum">' + hi(r.sm, list) + "</p>" : "")
      + (tags ? '<div class="tags">' + tags + "</div>" : "")
      + dup
      + "</article>";
  }

  function renderList() {
    var list = terms(ST.q);
    var html, j, n = Math.min(LIMIT, RESULT.length);

    if (!RESULT.length) {
      listEl.innerHTML = '<p class="empty">ไม่พบข่าวที่ตรงกับเงื่อนไขนี้ '
        + "— ลองลดคำค้นหรือขยายช่วงเวลา</p>";
      show($("morewrap"), false);
      return;
    }

    if (ST.v === "tl") {
      /* ไทม์ไลน์: ชุดผลลัพธ์เดิม เรียงเก่า -> ใหม่ จัดกลุ่มตามวัน */
      var asc = RESULT.slice(0).sort(function (x, y) {
        return x.d < y.d ? -1 : x.d > y.d ? 1 : 0;
      }).slice(0, n);
      html = '<div class="tl">';
      var day = null;
      for (j = 0; j < asc.length; j++) {
        var key = asc[j].d.slice(0, 10);
        if (key !== day) {
          if (day !== null) { html += "</ol>"; }
          day = key;
          html += '<div class="day">' + esc(thaiDay(key)) + '</div><ol class="feed">';
        }
        html += "<li>" + card(asc[j], list) + "</li>";
      }
      if (day !== null) { html += "</ol>"; }
      listEl.innerHTML = html + "</div>";
    } else {
      html = '<ol class="feed">';
      for (j = 0; j < n; j++) { html += "<li>" + card(RESULT[j], list) + "</li>"; }
      listEl.innerHTML = html + "</ol>";
    }

    show($("morewrap"), RESULT.length > n);
    $("more").textContent = "โหลดเพิ่ม (เหลืออีก " + nf(RESULT.length - n) + ")";
  }

  function counter() {
    var undated = 0, j;
    for (j = 0; j < RESULT.length; j++) { if (RESULT[j].df) { undated++; } }
    var text = "พบ " + nf(RESULT.length) + " ชิ้น จาก " + nf(ROWS.length);
    var weak = [];
    if (undated) {
      weak.push(nf(undated) + " ชิ้นไม่มีวันที่ตีพิมพ์ (แสดงเวลาที่ระบบพบข่าว)");
    }
    if (ST.m) { weak.push("เฉพาะเดือน " + ST.m); }
    $("cnt").innerHTML = esc(text)
      + (weak.length ? ' <span class="weak">· ' + esc(weak.join(" · ")) + "</span>" : "");
  }

  function selbar() {
    var n = 0;
    for (var key in SEL) { if (SEL[key]) { n++; } }
    show($("selbar"), n > 0);
    $("seln").textContent = "เลือกไว้ " + nf(n) + " ชิ้น";
  }

  function apply(keepLimit) {
    if (!keepLimit) { LIMIT = PAGE_STEP; }
    var base = baseSet();
    chart(base);
    RESULT = ST.m
      ? base.filter(function (r) { return r.d.slice(0, 7) === ST.m; })
      : base;
    syncFilters();
    counter();
    renderList();
    selbar();
    writeState();
  }

  /* ------------------------------------------------------------ สถานะใน URL */

  function writeState() {
    var p = [], j, on = [];
    if (ST.q) { p.push("q=" + encodeURIComponent(ST.q)); }
    for (j = 0; j < LVKEYS.length; j++) { if (ST.lv[LVKEYS[j]]) { on.push(LVKEYS[j]); } }
    if (on.length) { p.push("lv=" + on.join(",")); }
    on = [];
    for (j = 0; j < D.top.length; j++) { if (ST.tp[j]) { on.push(j); } }
    if (on.length) { p.push("t=" + on.join(",")); }
    if (ST.rg !== "all") { p.push("r=" + ST.rg); }
    if (ST.from) { p.push("from=" + ST.from); }
    if (ST.to) { p.push("to=" + ST.to); }
    if (ST.src >= 0) { p.push("s=" + ST.src); }
    if (ST.v !== "list") { p.push("v=" + ST.v); }
    if (ST.m) { p.push("m=" + ST.m); }
    /* file:// เป็น origin ทึบ บางเบราว์เซอร์ห้าม replaceState — ล้มก็แค่ไม่ซิงก์ */
    try { history.replaceState(null, "", "#" + p.join("&")); } catch (err) { /* ไม่เป็นไร */ }
  }

  function readState() {
    var raw = String(location.href).split("#")[1] || "";
    if (!raw) { return; }
    var parts = raw.split("&"), j, at, key, val, bits, m;
    for (j = 0; j < parts.length; j++) {
      at = parts[j].indexOf("=");
      if (at < 0) { continue; }
      key = parts[j].slice(0, at);
      try { val = decodeURIComponent(parts[j].slice(at + 1).replace(/\+/g, " ")); }
      catch (err) { val = parts[j].slice(at + 1); }
      if (key === "q") { ST.q = val; }
      else if (key === "lv") {
        bits = val.split(",");
        for (m = 0; m < bits.length; m++) { if (DOT[bits[m]]) { ST.lv[bits[m]] = true; } }
      } else if (key === "t") {
        bits = val.split(",");
        for (m = 0; m < bits.length; m++) {
          var ti = parseInt(bits[m], 10);
          if (ti >= 0 && ti < D.top.length) { ST.tp[ti] = true; }
        }
      } else if (key === "r" && /^(7|30|90|all|custom)$/.test(val)) { ST.rg = val; }
      else if (key === "from" && /^\d{4}-\d{2}-\d{2}$/.test(val)) { ST.from = val; ST.rg = "custom"; }
      else if (key === "to" && /^\d{4}-\d{2}-\d{2}$/.test(val)) { ST.to = val; ST.rg = "custom"; }
      else if (key === "s") {
        var si = parseInt(val, 10);
        ST.src = (si >= 0 && si < D.src.length) ? si : -1;
      } else if (key === "v" && (val === "tl" || val === "list")) { ST.v = val; }
      else if (key === "m" && /^\d{4}-\d{2}$/.test(val)) { ST.m = val; }
    }
  }

  /* --------------------------------------------------------- ส่งออก/คัดลอก */

  function picked() {
    var out = [];
    for (var j = 0; j < ROWS.length; j++) { if (SEL[ROWS[j].id]) { out.push(ROWS[j]); } }
    return out;
  }

  function topicNames(r) {
    var out = [];
    for (var j = 0; j < r.tp.length; j++) { out.push(D.top[r.tp[j]] || ""); }
    return out.join(" / ");
  }

  function cell(v) { return '"' + String(v == null ? "" : v).replace(/"/g, '""') + '"'; }

  function toCsv(rows) {
    var head = ["วันที่", "เวลา", "พาดหัว", "แหล่ง", "ระดับ", "หัวข้อ", "ลิงก์", "สรุป"];
    var lines = [head.map(cell).join(",")], j;
    for (j = 0; j < rows.length; j++) {
      var r = rows[j];
      lines.push([
        dmy(r.d) + (r.df ? " (เวลาที่ระบบพบข่าว)" : ""),
        hhmm(r.d),
        r.t,
        r.s,
        SHOW_LEVEL ? (DOT[r.lv] ? DOT[r.lv] + " " + LVNAME[r.lv] : "") : "",
        topicNames(r),
        safeUrl(r.u),
        r.sm
      ].map(cell).join(","));
    }
    /* BOM นำหน้า ไม่งั้น Excel ไทยเปิดมาเป็นขยะ */
    return "\ufeff" + lines.join("\r\n") + "\r\n";
  }

  function toText(rows) {
    var out = [], j;
    for (j = 0; j < rows.length; j++) {
      var r = rows[j];
      var line = when(r.d) + (r.df ? " (เวลาที่ระบบพบข่าว)" : "")
        + (r.s ? " · " + r.s : "");
      out.push(line + "\n" + r.t + (safeUrl(r.u) ? "\n" + safeUrl(r.u) : ""));
    }
    return out.join("\n\n");
  }

  function openBox(msg, text) {
    $("boxmsg").textContent = msg;
    $("boxta").value = text;
    show($("box"), true);
    try { $("boxta").focus(); $("boxta").select(); } catch (err) { /* ไม่เป็นไร */ }
  }

  function toast(msg) {
    $("toastmsg").textContent = msg;
    show($("toast"), true);
    setTimeout(function () { show($("toast"), false); }, 1800);
  }

  function save(name, text, kind) {
    var link = document.createElement("a");
    var can = ("download" in link) && window.Blob && window.URL && URL.createObjectURL;
    if (!can) {
      openBox("เบราว์เซอร์นี้บันทึกไฟล์เองไม่ได้ — กดเลือกทั้งหมดแล้วคัดลอกไปวางใน"
        + "โปรแกรมตารางแทน", text);
      return;
    }
    try {
      var blob = new Blob([text], { type: kind });
      link.href = URL.createObjectURL(blob);
      link.download = name;
      document.body.appendChild(link);
      link.click();
      setTimeout(function () {
        try { URL.revokeObjectURL(link.href); } catch (err) { /* ไม่เป็นไร */ }
        if (link.parentNode) { link.parentNode.removeChild(link); }
      }, 0);
      toast("ส่งออกแล้ว");
    } catch (err) {
      openBox("ดาวน์โหลดไม่สำเร็จ — กดเลือกทั้งหมดแล้วคัดลอกไปวางแทน", text);
    }
  }

  function copyOut(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast("คัดลอกแล้ว");
      }, function () {
        openBox("คัดลอกอัตโนมัติไม่ได้ — กดเลือกทั้งหมดแล้วคัดลอกเอง", text);
      });
    } else {
      openBox("คัดลอกอัตโนมัติไม่ได้ — กดเลือกทั้งหมดแล้วคัดลอกเอง", text);
    }
  }

  /* ------------------------------------------------------------------ ธีม */

  function theme(mode) {
    if (mode) { document.documentElement.setAttribute("data-theme", mode); }
    else { document.documentElement.removeAttribute("data-theme"); }
  }

  function initTheme() {
    var saved = "";
    try { saved = localStorage.getItem("steel-archive-theme") || ""; }
    catch (err) { saved = ""; }
    if (saved === "dark" || saved === "light") { theme(saved); }
  }

  function flipTheme() {
    var now = document.documentElement.getAttribute("data-theme");
    var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    var next = now ? (now === "dark" ? "light" : "dark") : (dark ? "light" : "dark");
    theme(next);
    try { localStorage.setItem("steel-archive-theme", next); } catch (err) { /* ไม่เป็นไร */ }
  }

  /* ---------------------------------------------------------------- เหตุการณ์ */

  function climb(node, root, test) {
    while (node && node !== root) {
      if (test(node)) { return node; }
      node = node.parentNode;
    }
    return null;
  }

  function hasClass(node, name) {
    return node.className && (" " + node.className + " ").indexOf(" " + name + " ") >= 0;
  }

  function toggleTopic(idx) {
    if (ST.tp[idx]) { delete ST.tp[idx]; } else { ST.tp[idx] = true; }
    apply();
  }

  $("qi").addEventListener("input", function () {
    var v = this.value;
    if (TIMER) { clearTimeout(TIMER); }
    TIMER = setTimeout(function () { ST.q = v; apply(); }, 120);
  });

  $("clr").addEventListener("click", function () {
    $("qi").value = "";
    ST.q = "";
    apply();
  });

  $("lvrow").addEventListener("click", function (ev) {
    var b = climb(ev.target, this, function (n) { return n.getAttribute && n.getAttribute("data-lv"); });
    if (!b) { return; }
    var key = b.getAttribute("data-lv");
    if (key === "*") { ST.lv = {}; }
    else if (ST.lv[key]) { delete ST.lv[key]; }
    else { ST.lv[key] = true; }
    apply();
  });

  $("rgrow").addEventListener("click", function (ev) {
    var b = climb(ev.target, this, function (n) { return n.getAttribute && n.getAttribute("data-rg"); });
    if (!b) { return; }
    ST.rg = b.getAttribute("data-rg");
    ST.m = "";
    apply();
  });

  $("tprow").addEventListener("click", function (ev) {
    var b = climb(ev.target, this, function (n) { return n.getAttribute && n.getAttribute("data-tp"); });
    if (!b) { return; }
    var key = b.getAttribute("data-tp");
    if (key === "*") { ST.tp = {}; apply(); } else { toggleTopic(parseInt(key, 10)); }
  });

  $("dfrom").addEventListener("change", function () { ST.from = this.value; ST.rg = "custom"; apply(); });
  $("dto").addEventListener("change", function () { ST.to = this.value; ST.rg = "custom"; apply(); });

  $("srcsel").addEventListener("change", function () { ST.src = parseInt(this.value, 10); apply(); });

  $("vlist").addEventListener("click", function () { ST.v = "list"; apply(); });
  $("vtl").addEventListener("click", function () { ST.v = "tl"; apply(); });

  $("chart").addEventListener("click", function (ev) {
    var b = climb(ev.target, this, function (n) { return n.getAttribute && n.getAttribute("data-m"); });
    if (!b) { return; }
    var key = b.getAttribute("data-m");
    ST.m = (ST.m === key) ? "" : key;
    apply();
  });

  listEl.addEventListener("click", function (ev) {
    var b = climb(ev.target, this, function (n) { return hasClass(n, "tchip"); });
    if (b) { toggleTopic(parseInt(b.getAttribute("data-tp"), 10)); }
  });

  listEl.addEventListener("change", function (ev) {
    var t = ev.target;
    if (!t || !hasClass(t, "pick")) { return; }
    var id = t.getAttribute("data-id");
    if (t.checked) { SEL[id] = true; } else { delete SEL[id]; }
    var host = climb(t, listEl, function (n) { return hasClass(n, "card"); });
    if (host) { host.className = t.checked ? "card picked" : "card"; }
    selbar();
  });

  $("more").addEventListener("click", function () {
    LIMIT += PAGE_STEP;
    renderList();
  });

  $("bcsv").addEventListener("click", function () {
    save("คลังข่าวเหล็ก.csv", toCsv(picked()), "text/csv;charset=utf-8");
  });

  $("bcopy").addEventListener("click", function () { copyOut(toText(picked())); });

  $("bclr").addEventListener("click", function () {
    SEL = {};
    renderList();
    selbar();
  });

  $("boxsel").addEventListener("click", function () {
    try { $("boxta").focus(); $("boxta").select(); } catch (err) { /* ไม่เป็นไร */ }
  });

  $("boxclose").addEventListener("click", function () { show($("box"), false); });

  $("theme").addEventListener("click", flipTheme);

  $("totop").addEventListener("click", function () {
    try { window.scrollTo(0, 0); } catch (err) { /* ไม่เป็นไร */ }
  });

  /* ------------------------------------------------------------------ เริ่ม */

  initTheme();
  readState();
  buildFilters();
  $("qi").value = ST.q;
  $("dfrom").value = ST.from;
  $("dto").value = ST.to;
  header();
  apply();
})();
