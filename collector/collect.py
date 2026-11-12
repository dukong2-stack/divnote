"""
배당가치 노트 — 수집·스크리닝 스크립트

데이터 출처
  - 주가·시가총액: 공공데이터포털 '금융위원회_주식시세정보' (DATA_GO_KR_KEY)
  - 재무·배당·업종·현금흐름: OpenDART (DART_API_KEY)

결과물 (data/)
  latest.json    선정 후보 상세(점수·선정 사유·주의사항·연도별 수치)
  universe.json  평가한 전체 종목의 간단 정보와 탈락 사유
  prices.json    코스피·코스닥 전 종목 최근 종가 (포트폴리오 평가용)
  history/       월별 후보 스냅샷 (선정 이후 성과 검증용)
  cache/         변하지 않는 공시 데이터 캐시 (호출 수 절약)
"""
import os, io, re, sys, json, time, math, zipfile, statistics, datetime as dt
from pathlib import Path
from urllib.parse import unquote
import xml.etree.ElementTree as ET
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
HIST = DATA / "history"
for p in (DATA, CACHE, HIST):
    p.mkdir(parents=True, exist_ok=True)

DART = "https://opendart.fss.or.kr/api"
PRICE_URL = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
DART_KEY = os.environ.get("DART_API_KEY", "").strip()
GO_KEY = unquote(os.environ.get("DATA_GO_KR_KEY", "").strip())  # Encoding 키를 넣어도 동작하도록
SLEEP = float(os.environ.get("DART_SLEEP", "0.12"))
KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)
S = requests.Session()
S.headers["User-Agent"] = "divnote/1.0"
CALLS = {"dart": 0}

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

SECTORS = {
    "01": "농업", "02": "임업", "03": "어업", "05": "석탄·원유", "06": "금속광업", "07": "비금속광업", "08": "광업지원",
    "10": "식료품", "11": "음료", "12": "담배", "13": "섬유", "14": "의복", "15": "가죽·신발", "16": "목재",
    "17": "펄프·종이", "18": "인쇄", "19": "석유정제", "20": "화학", "21": "의약품", "22": "고무·플라스틱",
    "23": "비금속광물", "24": "1차금속", "25": "금속가공", "26": "전자부품·통신장비", "27": "의료·정밀기기",
    "28": "전기장비", "29": "기계", "30": "자동차·부품", "31": "조선·운송장비", "32": "가구", "33": "기타제조",
    "35": "전기·가스", "36": "수도", "37": "하수처리", "38": "폐기물·재활용", "41": "종합건설", "42": "전문건설",
    "45": "자동차판매", "46": "도매", "47": "소매", "49": "육상운송", "50": "해운", "51": "항공운송",
    "52": "물류·창고", "55": "숙박", "56": "음식점", "58": "출판·소프트웨어", "59": "영상·음악", "60": "방송",
    "61": "통신", "62": "IT서비스", "63": "정보서비스", "64": "금융", "65": "보험", "66": "금융서비스",
    "68": "부동산", "70": "연구개발", "71": "전문서비스", "72": "엔지니어링", "73": "과학기술서비스",
    "74": "시설관리", "75": "사업지원", "84": "공공행정", "85": "교육", "86": "보건", "87": "사회복지",
    "90": "예술", "91": "스포츠·오락", "94": "협회·단체", "95": "수리", "96": "개인서비스",
}
FINANCIAL = {"64", "65", "66"}
CYCLICAL = {"05", "06", "07", "08", "19", "20", "24", "31", "41", "42", "50"}


def log(*a):
    print(*a, flush=True)


# ───────────────────────── 공통 유틸 ─────────────────────────
def num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "N/A", "nan"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def load_cache(name):
    f = CACHE / f"{name}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(name, obj):
    (CACHE / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_json(path, obj, pretty=False):
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=1 if pretty else None, separators=None if pretty else (",", ":")),
        encoding="utf-8",
    )


def dart(path, params, raw=False):
    """OpenDART 호출. 데이터 없음(013)은 None, 키·한도 오류는 즉시 중단."""
    q = {"crtfc_key": DART_KEY, **params}
    last = None
    for i in range(4):
        try:
            r = S.get(f"{DART}/{path}", params=q, timeout=40)
            CALLS["dart"] += 1
            time.sleep(SLEEP)
            if raw:
                r.raise_for_status()
                return r.content
            j = r.json()
            st = j.get("status")
            if st == "000":
                return j
            if st in ("013", "014"):
                return None
            if st in ("010", "011", "012", "901"):
                sys.exit(f"[중단] OpenDART 인증키 문제({st}): {j.get('message')} — 저장소 Secrets의 DART_API_KEY를 확인하세요.")
            if st == "020":
                sys.exit("[중단] OpenDART 일일 호출 한도를 넘었습니다. 내일 다시 실행하면 캐시 덕분에 이어서 진행됩니다.")
            if st == "800":
                sys.exit("[중단] OpenDART 시스템 점검 중입니다.")
            last = f"{st} {j.get('message')}"
            log(f"  DART {path} 응답 {last}")
            return None
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(2 * (i + 1))
    log(f"  DART {path} 실패: {last}")
    return None


# ───────────────────────── 1. 주가 ─────────────────────────
NAVER_URL = "https://m.stock.naver.com/api/stocks/marketValue/{mkt}"


def fetch_prices_go():
    """공공데이터포털 주식시세. 실패하면 예외를 던져 네이버로 넘어감"""
    if not GO_KEY:
        raise RuntimeError("DATA_GO_KR_KEY 없음")
    for back in range(0, 12):
        d = (NOW - dt.timedelta(days=back)).strftime("%Y%m%d")
        rows, page, total = [], 1, 0
        while True:
            r = S.get(PRICE_URL, params={
                "serviceKey": GO_KEY, "resultType": "json", "numOfRows": 5000, "pageNo": page, "basDt": d,
            }, timeout=60)
            text = r.text.strip()
            try:
                j = r.json()
            except ValueError:
                raise RuntimeError(f"JSON 아님 (HTTP {r.status_code}): {text[:300]}")
            resp = j.get("response", j) if isinstance(j, dict) else {}
            hdr = resp.get("header") or {}
            body = resp.get("body")
            if body is None or str(hdr.get("resultCode", "00")) != "00":
                raise RuntimeError(f"HTTP {r.status_code}: {text[:300]}")
            total = int(body.get("totalCount") or 0)
            items = body.get("items") or {}
            items = items.get("item", []) if isinstance(items, dict) else items
            if isinstance(items, dict):
                items = [items]
            rows += items
            if len(rows) >= total or not items:
                break
            page += 1
        if rows:
            return d, rows
    raise RuntimeError("최근 12일 동안 데이터 없음")


def fetch_prices_naver():
    """네이버 증권 공개 시세(시가총액순). 인증키 불필요"""
    hdr = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
    rows = []
    for mkt in ("KOSPI", "KOSDAQ"):
        for page in range(1, 61):
            try:
                r = S.get(NAVER_URL.format(mkt=mkt), params={"page": page, "pageSize": 100}, headers=hdr, timeout=30)
                j = r.json()
            except Exception as e:
                sys.exit(f"[중단] 네이버 시세 응답을 읽지 못했습니다 ({mkt} {page}쪽): {e}")
            items = j.get("stocks") if isinstance(j, dict) else j
            if not items:
                break
            for it in items:
                code = str(it.get("itemCode") or "").strip()
                if len(code) != 6:
                    continue
                if str(it.get("stockEndType") or "stock").lower() != "stock":
                    continue  # ETF·ETN 등 제외
                mv = num(it.get("marketValue"))
                if mv is not None and mv < 1e9:  # 억원 단위면 원으로 환산
                    mv *= 1e8
                rows.append({"srtnCd": code, "itmsNm": it.get("stockName") or it.get("itemName"),
                             "clpr": num(it.get("closePrice")), "mrktTotAmt": mv, "mrktCtg": mkt})
            total = j.get("totalCount") if isinstance(j, dict) else None
            if total and page * 100 >= int(total):
                break
            time.sleep(0.2)
    if len(rows) < 1000:
        sys.exit(f"[중단] 네이버 시세가 {len(rows)}종목만 수집되었습니다. 이 로그를 보내 주세요.")
    d = NOW - dt.timedelta(days=1) if NOW.hour < 16 else NOW  # 장 마감 전 실행이면 직전 거래일 종가
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.strftime("%Y%m%d"), rows


def fetch_prices():
    try:
        d, rows = fetch_prices_go()
        log(f"주가(공공데이터포털) 기준일 {d}: {len(rows)}종목")
        return d, rows
    except Exception as e:
        log(f"공공데이터포털 실패 → 네이버 시세로 전환 ({str(e)[:150]})")
    d, rows = fetch_prices_naver()
    log(f"주가(네이버) {d}: {len(rows)}종목")
    return d, rows


# ───────────────────────── 2. 고유번호 ─────────────────────────
def corp_codes():
    f = CACHE / "corpcode.json"
    if f.exists() and (time.time() - f.stat().st_mtime) < 7 * 86400:
        return json.loads(f.read_text(encoding="utf-8"))
    raw = dart("corpCode.xml", {}, raw=True)
    if not raw:
        sys.exit("[중단] OpenDART 고유번호 파일을 받지 못했습니다.")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        sys.exit(f"[중단] 고유번호 파일 형식 오류: {raw[:200]!r} — 인증키를 확인하세요.")
    root = ET.fromstring(z.read(z.namelist()[0]))
    m = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = el.findtext("corp_code").strip()
    f.write_text(json.dumps(m), encoding="utf-8")
    return m


# ───────────────────────── 3. 재무 주요계정 ─────────────────────────
ACC = {
    "rev": ("매출액", "영업수익", "수익(매출액)"),
    "op": ("영업이익",),
    "ni": ("당기순이익",),
    "assets": ("자산총계",),
    "liab": ("부채총계",),
    "equity": ("자본총계",),
}


def pick_acc(nm):
    for k, keys in ACC.items():
        if any(x in nm for x in keys):
            if k == "rev" and "원가" in nm:
                continue
            return k
    return None


def multi_accounts(corps, year, reprt):
    """다중회사 주요계정(100개씩). 반환: {corp: {acc: [당기, 전기, 전전기], 'q': {...}}}"""
    name = f"fin2_{year}_{reprt}" if reprt == "11011" else f"finq_{year}_{reprt}"
    cache = load_cache(name)
    todo = [c for c in corps if c not in cache]
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        j = dart("fnlttMultiAcnt.json", {"corp_code": ",".join(chunk), "bsns_year": str(year), "reprt_code": reprt})
        if not j:
            continue
        per = {}
        for it in j.get("list", []):
            per.setdefault(it["corp_code"], []).append(it)
        for c, items in per.items():
            has_cfs = any(it.get("fs_div") == "CFS" for it in items)
            out = {}
            for it in items:
                if reprt == "11011" and has_cfs and it.get("fs_div") == "OFS" and pick_acc(it.get("account_nm", "")) == "rev" and "rev_ofs" not in out:
                    out["rev_ofs"] = [num(it.get("thstrm_amount")), num(it.get("frmtrm_amount")), num(it.get("bfefrmtrm_amount"))]
                if (it.get("fs_div") == "CFS") != has_cfs:
                    continue
                k = pick_acc(it.get("account_nm", ""))
                if not k or k in out:
                    continue
                if reprt == "11011":
                    out[k] = [num(it.get("thstrm_amount")), num(it.get("frmtrm_amount")), num(it.get("bfefrmtrm_amount"))]
                else:  # 분기·반기: 누적 금액 우선
                    cur = num(it.get("thstrm_add_amount")) if it.get("thstrm_add_amount") not in (None, "", "-") else num(it.get("thstrm_amount"))
                    prv = num(it.get("frmtrm_add_amount")) if it.get("frmtrm_add_amount") not in (None, "", "-") else num(it.get("frmtrm_amount"))
                    q_cur = num(it.get("thstrm_amount")) if it.get("thstrm_add_amount") not in (None, "", "-") else None
                    q_prv = num(it.get("frmtrm_amount")) if it.get("frmtrm_add_amount") not in (None, "", "-") else None
                    out[k] = [cur, prv, q_cur, q_prv]  # [당기 누적, 전기 누적, 당기 3개월, 전기 3개월]
            out["fs"] = "CFS" if has_cfs else "OFS"
            cache[c] = out
        save_cache(name, cache)
        log(f"  주요계정 {year}/{reprt}: {min(i + 100, len(todo))}/{len(todo)}")
    return cache


# ───────────────────────── 4. 배당 ─────────────────────────
def dividends(corp, year, cache):
    key = f"{corp}_{year}"
    if key in cache:
        return cache[key]
    j = dart("alotMatter.json", {"corp_code": corp, "bsns_year": str(year), "reprt_code": "11011"})
    out = {"dps": [None] * 3, "yld": [None] * 3, "payout": [None] * 3, "eps": [None] * 3, "total": [None] * 3}
    if j:
        for it in j.get("list", []):
            se = (it.get("se") or "").replace(" ", "")
            kind = (it.get("stock_knd") or "").strip()
            vals = [num(it.get("thstrm")), num(it.get("frmtrm")), num(it.get("lwfr"))]
            if "주당현금배당금" in se and kind in ("", "보통주"):
                out["dps"] = vals
            elif "현금배당수익률" in se and kind in ("", "보통주"):
                out["yld"] = vals
            elif "현금배당성향" in se:
                out["payout"] = vals
            elif "주당순이익" in se and out["eps"] == [None] * 3:
                out["eps"] = vals
            elif "현금배당금총액" in se:
                out["total"] = vals
    cache[key] = out
    return out


# ───────────────────────── 5. 업종·현금흐름 ─────────────────────────
def company(corp, cache):
    if corp in cache:
        return cache[corp]
    j = dart("company.json", {"corp_code": corp})
    out = {"ksic": (j or {}).get("induty_code", ""), "cls": (j or {}).get("corp_cls", "")}
    cache[corp] = out
    return out


def fsall(corp, year, reprt, cache):
    """전체 재무제표에서 필요한 값만 추출 (연결 우선, 없으면 별도)"""
    key = f"{corp}_{year}_{reprt}"
    if key in cache:
        return cache[key]
    out = None
    annual = reprt == "11011"
    for fs in ("CFS", "OFS"):
        j = dart("fnlttSinglAcntAll.json", {"corp_code": corp, "bsns_year": str(year), "reprt_code": reprt, "fs_div": fs})
        if not j:
            continue
        o = {"fs": fs, "ocf": None, "capex": None, "ctrl_ni": None, "ctrl_eq": None, "cash": 0.0, "debt": 0.0, "fin_like": False}
        has_cash = False
        for it in j.get("list", []):
            aid = it.get("account_id", "")
            nm = (it.get("account_nm") or "").replace(" ", "")
            sj = it.get("sj_div")
            if annual:
                vals = [num(it.get("thstrm_amount")), num(it.get("frmtrm_amount")), num(it.get("bfefrmtrm_amount"))]
            else:
                add = it.get("thstrm_add_amount") not in (None, "", "-")
                vals = [num(it.get("thstrm_add_amount")) if add else num(it.get("thstrm_amount")),
                        num(it.get("frmtrm_add_amount")) if it.get("frmtrm_add_amount") not in (None, "", "-") else num(it.get("frmtrm_amount"))]
            if sj == "CF":
                if o["ocf"] is None and (aid == "ifrs-full_CashFlowsFromUsedInOperatingActivities" or (nm.startswith("영업활동") and "현금흐름" in nm)):
                    o["ocf"] = vals
                if o["capex"] is None and (aid == "ifrs-full_PurchaseOfPropertyPlantAndEquipment" or "유형자산의취득" in nm):
                    o["capex"] = [abs(v) if v is not None else None for v in vals]
            elif sj in ("IS", "CIS"):
                if o["ctrl_ni"] is None and (aid == "ifrs-full_ProfitLossAttributableToOwnersOfParent" or ("지배기업" in nm and "비지배" not in nm and ("순이익" in nm or "순손익" in nm))):
                    o["ctrl_ni"] = vals
            elif sj == "BS":
                v = vals[0]
                if o["ctrl_eq"] is None and (aid == "ifrs-full_EquityAttributableToOwnersOfParent" or ("지배기업" in nm and "비지배" not in nm and "지분" in nm)):
                    o["ctrl_eq"] = v
                if v is not None:
                    if aid == "ifrs-full_CashAndCashEquivalents" or nm == "현금및현금성자산":
                        o["cash"] += v; has_cash = True
                    elif "단기금융상품" in nm or nm == "단기금융자산":
                        o["cash"] += v
                    elif ("차입금" in nm or ("사채" in nm and "이자" not in nm and "할인" not in nm)) and "리스" not in nm and "합계" not in nm:
                        o["debt"] += v
                if any(x in nm for x in ("예수부채", "투자자예수금", "고객예탁금", "고객예수금")):
                    o["fin_like"] = True
        if not has_cash:
            o["cash"] = None
        out = o
        break
    cache[key] = out
    return out


def div_cum(corp, year, reprt, cache):
    """분·반기보고서 '배당에 관한 사항'의 올해 누적 주당 현금배당금(보통주). 보고서가 없으면 None"""
    key = f"{corp}_{year}_{reprt}"
    if key in cache:
        return cache[key]
    j = dart("alotMatter.json", {"corp_code": corp, "bsns_year": str(year), "reprt_code": reprt})
    out = None
    if j:
        out = 0.0
        for it in j.get("list", []):
            se = (it.get("se") or "").replace(" ", "")
            if "주당현금배당금" in se and (it.get("stock_knd") or "").strip() in ("", "보통주"):
                out = num(it.get("thstrm")) or 0.0
                break
    cache[key] = out
    return out


def div_progress(rec, qcands, cache):
    """올해 결정된 배당: 누적, 작년 같은 시점 누적, 최근 분기 배당과 삭감 여부"""
    latest = cum = None
    for i, qc in enumerate(qcands):
        c = div_cum(rec["corp"], qc[0], qc[1], cache)
        if c is not None:
            latest, cum, idx = qc, c, i
            break
    if latest is None or not cum:
        return None
    out = {"label": latest[2], "nq": latest[3], "cum": cum,
           "prev_same": div_cum(rec["corp"], latest[0] - 1, latest[1], cache) or 0.0}
    earlier = qcands[idx + 1:]
    if earlier:
        c1 = div_cum(rec["corp"], earlier[0][0], earlier[0][1], cache) or 0.0
        if c1 > cum:  # 누적이 아닌 경우: 판단 보류
            return None
        c2 = div_cum(rec["corp"], earlier[1][0], earlier[1][1], cache) if len(earlier) > 1 else None
        quarterly = c1 > 0 and (c2 is None or c2 > 0)
        if quarterly:
            out["q_last"] = cum - c1
            out["q_prev_avg"] = c1 / earlier[0][3]
            out["cut"] = out["q_last"] < out["q_prev_avg"] * 0.9
    return out


def norm_name(n):
    return (n or "").replace("(주)", "").replace("주식회사", "").replace("㈜", "").replace(" ", "").strip()


def nav_value(rec, year, cache, name_map):
    """사업보고서 '타법인 출자 현황'의 상장사 지분가치 합계(억원)"""
    key = f"{rec['corp']}_{year}"
    if key not in cache:
        j = dart("otrCprInvstmntSttus.json", {"corp_code": rec["corp"], "bsns_year": str(year), "reprt_code": "11011"})
        rows = []
        for it in (j or {}).get("list", []):
            rt = num(it.get("trmend_blce_qota_rt"))
            if rt and rt > 0:
                rows.append([it.get("inv_prm"), rt])
        cache[key] = rows
    total, items = 0.0, []
    for nm, rt in cache[key]:
        hit = name_map.get(norm_name(nm))
        if not hit or hit[0] == rec["code"]:
            continue
        val = (hit[1][2] or 0) * rt / 100  # 억원
        if val > 0:
            total += val
            items.append({"n": hit[1][0], "c": hit[0], "rt": r1(rt, 2), "v": r1(val, 0)})
    items.sort(key=lambda x: -x["v"])
    return {"total": r1(total, 0), "items": items[:5], "codes": [x["c"] for x in items]} if total > 0 else None


def quarter_candidates():
    md = NOW.month * 100 + NOW.day
    return [x for x in ((NOW.year, "11014", "3분기보고서", 3, 1101), (NOW.year, "11012", "반기보고서", 2, 801),
                        (NOW.year, "11013", "1분기보고서", 1, 501)) if md >= x[4]]


def shares_total(corp, year, cache):
    """사업보고서 '주식의 총수 현황'의 보통주 발행주식총수"""
    key = f"{corp}_{year}"
    if key in cache:
        return cache[key]
    j = dart("stockTotqySttus.json", {"corp_code": corp, "bsns_year": str(year), "reprt_code": "11011"})
    out = None
    for it in (j or {}).get("list", []):
        if (it.get("se") or "").replace(" ", "") == "보통주":
            out = num(it.get("istc_totqy"))
            break
    cache[key] = out
    return out


# ───────────────────────── 5-2. 컨센서스·거래대금·잠정실적·우선주 ─────────────────────────
NHDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": "https://finance.naver.com/"}


def _cells(row):
    return [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
            for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S | re.I)]


def naver_consensus(code, year):
    """네이버 종목 화면 '기업실적분석' 표의 올해 추정(E) 영업이익·순이익(억원)·EPS(원). 없으면 None"""
    try:
        r = S.get("https://finance.naver.com/item/main.naver", params={"code": code}, headers=NHDR, timeout=20)
        html = r.content.decode("euc-kr", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r'class="section cop_analysis".*?<table[^>]*>(.*?)</table>', html, re.S)
    if not m:
        return None
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S | re.I)
    col = None
    for row in rows[:3]:
        hs = _cells(row)
        for j, h in enumerate(hs[:4]):
            if "(E)" in h and h.startswith(str(year)):
                col = j
        if col is not None:
            break
    if col is None:
        return None
    out = {}
    for row in rows:
        cs = _cells(row)
        if len(cs) < 5:
            continue
        lab = cs[0].replace(" ", "")
        vals = cs[1:]
        key = "op" if lab == "영업이익" else "ni" if lab == "당기순이익" else "eps" if lab.startswith("EPS") else None
        if key and key not in out and col < len(vals):
            out[key] = num(vals[col])
    return out if out.get("eps") or out.get("op") else None


def trading_value(code):
    """최근 20거래일 평균 거래대금(억원, 종가×거래량)"""
    try:
        r = S.get("https://fchart.stock.naver.com/sise.nhn", params={"symbol": code, "timeframe": "day", "count": 20, "requestType": 0},
                  headers=NHDR, timeout=20)
        rows = re.findall(r'data="([^"]+)"', r.content.decode("euc-kr", errors="replace"))
        v = [num(x.split("|")[4]) * num(x.split("|")[5]) for x in rows if len(x.split("|")) >= 6 and num(x.split("|")[4]) and num(x.split("|")[5]) is not None]
        return sum(v) / len(v) / 1e8 if v else None
    except Exception:  # noqa: BLE001
        return None


def pref_factor(corp, year, cache):
    """(보통주+우선주) ÷ 보통주 발행주식수 — 우선주가 있으면 1보다 큼"""
    key = f"{corp}_{year}"
    if key not in cache:
        j = dart("stockTotqySttus.json", {"corp_code": corp, "bsns_year": str(year), "reprt_code": "11011"})
        com = pref = 0
        for it in (j or {}).get("list", []):
            se = (it.get("se") or "").replace(" ", "")
            v = num(it.get("istc_totqy")) or 0
            if se == "보통주":
                com = v
            elif "우선" in se or se == "종류주식":
                pref += v
        cache[key] = (com + pref) / com if com else 1.0
    return cache[key]


PRELIM_KEYS = {"매출액": "rev", "영업이익": "op", "당기순이익": "ni"}


def parse_prelim(txt):
    """잠정실적 공정공시 표 → {rev/op/ni: (당해, 전년동기)} 원 단위"""
    mu = re.search(r"단위\s*[:：]?\s*(억원|백만원|천원|원)", re.sub(r"<[^>]+>", " ", txt))
    if not mu:
        return None
    unit = {"억원": 1e8, "백만원": 1e6, "천원": 1e3, "원": 1}[mu.group(1)]
    found = {}
    for row in re.findall(r"<TR[^>]*>(.*?)</TR>", txt, re.S | re.I):
        cells = [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").replace("△", "-").replace("▲", "-").strip()
                 for c in re.findall(r"<(?:TD|TE|TH|TU)[^>]*>(.*?)</(?:TD|TE|TH|TU)>", row, re.S | re.I)]
        if not cells:
            continue
        lab = cells[0].replace(" ", "")
        key = next((v for k, v in PRELIM_KEYS.items() if lab.startswith(k)), None)
        if not key or key in found:
            continue
        rest = cells[1:]
        if rest and "누계" in rest[0]:
            continue
        if rest and "당해" in rest[0].replace(" ", ""):
            rest = rest[1:]
        n = [num(c) for c in rest]
        if len(n) < 5 or n[0] is None or n[3] is None:
            continue
        if n[4] is not None and n[3]:
            if abs((n[0] - n[3]) / abs(n[3]) * 100 - n[4]) > 2:
                continue
        found[key] = (n[0] * unit, n[3] * unit)
    return found if "op" in found else None


def prelim_update(rec, cache):
    """분기보고서보다 새 잠정실적이 있으면 분기 누적 자료(qrec)에 덧붙임"""
    j = dart("list.json", {"corp_code": rec["corp"], "bgn_de": (NOW - dt.timedelta(days=100)).strftime("%Y%m%d"),
                           "end_de": NOW.strftime("%Y%m%d"), "page_count": "100"})
    pre = [it for it in (j or {}).get("list", []) if "영업(잠정)실적" in (it.get("report_nm") or "").replace(" ", "")]
    if not pre:
        return
    it = max(pre, key=lambda x: x.get("rcept_dt", ""))
    d = it["rcept_dt"]
    y, m = int(d[:4]), int(d[4:6])
    yq = (y - 1, 4) if m <= 3 else (y, 1) if m <= 6 else (y, 2) if m <= 9 else (y, 3)
    qc, qrec = rec.get("qc"), rec.get("qrec") or {}
    if yq[1] == 4:
        return
    if qc and not (qc[0] == yq[0] and qc[3] == yq[1] - 1):
        return  # 이미 반영됐거나 이어지지 않는 분기
    if not qc and yq[1] != 1:
        return
    no = it["rcept_no"]
    if no not in cache:
        try:
            raw = dart("document.xml", {"rcept_no": no}, raw=True)
            z = zipfile.ZipFile(io.BytesIO(raw))
            txt = ""
            for nm in z.namelist():
                b = z.read(nm)
                for enc in ("utf-8", "cp949"):
                    try:
                        txt += b.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
            v = parse_prelim(txt)
            cache[no] = {k: list(x) for k, x in v.items()} if v else None
        except Exception as e:  # noqa: BLE001
            log(f"  잠정실적 읽기 실패 {rec['name']}: {e}")
            cache[no] = None
    v = cache[no]
    if not v:
        rec["prelim_fail"] = f"{yq[1]}분기 잠정실적 공시({d[4:6]}/{d[6:]})를 읽지 못해 반영 못 함 — 원문 확인"
        return
    new = dict(qrec)
    for k, (cur, prv) in v.items():
        old = qrec.get(k)
        if old and old[0] is not None and old[1] is not None:
            new[k] = [old[0] + cur, old[1] + prv, cur, prv]
        elif not qc:
            new[k] = [cur, prv, cur, prv]
    rec["qrec"] = new
    rec["qc"] = (yq[0], "prelim", f"{yq[1]}분기(잠정)", yq[1], 0)
    rec["prelim"] = f"{yq[1]}분기 잠정실적({d[4:6]}/{d[6:]} 공시) 반영"


def choose_est(rec, est, cons, TAX):
    """점수에 쓸 올해 추정: 컨센서스가 있으면 컨센서스, 없으면 최근 4개 분기·계절성·앱 추정 중 가장 보수적인 값.
    앱의 원래 추정은 est['app']에 남겨 '원래 추정으로 보기'에 씀"""
    if not est:
        return
    pf = rec.get("pref_f") or 1.0
    opn, nin = est.get("op"), est.get("ni")
    app = {"op": opn["base"] if opn else None, "op_vs": opn.get("vs_last") if opn else None,
           "per_fwd": r1(est["per_fwd"] * pf) if est.get("per_fwd") else None, "eps_fwd": est.get("eps_fwd"),
           "ni_vs": nin.get("vs_last") if nin else None}
    est["app"] = app
    if opn:
        opn["app_base"], opn["app_vs"] = opn["base"], opn.get("vs_last")
    sh = rec.get("shares_now")
    cr = (est.get("ctrl_ratio") or 100) / 100
    if cons and (cons.get("eps") or 0) > 0:
        est["src"] = "컨센서스"
        est["cons"] = cons
        if opn and cons.get("op") is not None:
            opn["base"] = r1(cons["op"], 0)
            opn["vs_last"] = r1((cons["op"] / opn["last"] - 1) * 100) if opn.get("last") and opn["last"] > 0 else None
        if nin and cons.get("ni") is not None:
            nin["base"] = r1(cons["ni"], 0)
            nin.pop("norm", None)
            nin["vs_last"] = r1((cons["ni"] / nin["last"] - 1) * 100) if nin.get("last") and nin["last"] > 0 else None
        est["eps_fwd"] = r1(cons["eps"], 0)
        est["per_fwd"] = r1(rec["price"] / cons["eps"])
    else:
        est["src"] = "보수 추정"
        fin = rec.get("fin")
        if opn:
            ttm = rec["ttm_op"] / 1e8 if rec.get("ttm_op") else None
            c = [x for x in ((ttm,) if fin else (opn["base"], opn.get("seasonal"), ttm)) if x is not None and x > 0]
            if c:
                opn["base"] = r1(min(c), 0)
                opn["vs_last"] = r1((opn["base"] / opn["last"] - 1) * 100) if opn.get("last") and opn["last"] > 0 else None
        if nin:
            ttm_c = rec.get("ttm_ni")
            app_c = rec["mcap"] * 1e8 / est["per_fwd"] if est.get("per_fwd") else None
            sea_c = nin["seasonal"] * 1e8 * cr if nin.get("seasonal") else None
            c = [x for x in ((ttm_c,) if fin else (app_c, sea_c, ttm_c)) if x is not None and x > 0]
            if c:
                f = min(c)
                est["per_fwd"] = r1(rec["mcap"] * 1e8 * pf / f)
                est["eps_fwd"] = r1(f / sh / pf, 0) if sh else est.get("eps_fwd")
                nin["base"] = r1(f / cr / 1e8, 0)
                nin.pop("norm", None)
                nin["vs_last"] = r1((nin["base"] / nin["last"] - 1) * 100) if nin.get("last") and nin["last"] > 0 else None
    o_vs = (opn or {}).get("vs_last")
    n_vs = (nin or {}).get("vs_last")
    est["g_eff"] = min([x for x in (o_vs, n_vs) if x is not None] or [None]) if (o_vs is not None or n_vs is not None) else None
    a_g = [x for x in (app["op_vs"], app["ni_vs"]) if x is not None]
    app["g_eff"] = min(a_g) if a_g else None
    est["alt"] = bool(app["per_fwd"] and est.get("per_fwd") and est["per_fwd"] > app["per_fwd"] * 1.1)
    # 이익률 상승 성격 단서: 매출이 함께 늘었는지
    rv = (est.get("rev") or {}).get("yoy")
    est["margin_hint"] = None
    if rec.get("q_margin") and rec.get("opm") and rec["opm"] > 0 and rec["q_margin"] >= rec["opm"] * 1.5:
        est["margin_hint"] = ("수요 증가형 — 매출도 {:+.0f}% 늘어 구조적일 가능성".format(rv) if rv is not None and rv >= 30
                              else "가격·원가형 — 매출은 {}인데 이익률만 올라 되돌림 위험".format(f"{rv:+.0f}%" if rv is not None else "변화 작음"))


# ───────────────────────── 6. 감사인 ─────────────────────────
def auditor(corp, year, reprt, cache):
    """회계감사인의 명칭 및 감사의견. 반환: {"cur": 당기(현재) 감사인}"""
    key = f"{corp}_{year}_{reprt}"
    if key in cache:
        return cache[key]
    j = dart("accnutAdtorNmNdAdtOpinion.json", {"corp_code": corp, "bsns_year": str(year), "reprt_code": reprt})
    out = None
    if j:
        cur = prev = None
        rows = [it for it in j.get("list", []) if (it.get("adtor") or "").strip() not in ("", "-")]
        for it in rows:
            period = (it.get("bsns_year") or "").replace(" ", "")
            if "당기" in period:
                cur = cur or it["adtor"].strip()
            elif "전기" in period and "전전기" not in period:
                prev = prev or it["adtor"].strip()
        if cur is None:  # '제57기 반기'처럼 당기 표시가 없으면 전기·전전기가 아닌 첫 행(당기)을 사용
            for it in rows:
                period = (it.get("bsns_year") or "").replace(" ", "")
                if "전기" not in period:
                    cur = it["adtor"].strip()
                    break
        if cur:
            out = {"cur": cur, "prev": prev}
    if out is not None or reprt == "11011":
        cache[key] = out  # 분기 보고서에서 못 찾은 경우는 다음에 다시 시도
    return out


# ───────────────────────── 지표 계산 ─────────────────────────
def series(fin_a, fin_b, acc):
    """[Y-5, ..., Y] 6개년 시계열"""
    a = (fin_a or {}).get(acc) or [None] * 3
    b = (fin_b or {}).get(acc) or [None] * 3
    return [b[2], b[1], b[0], a[2], a[1], a[0]]


def cagr(a, b, n):
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return (b / a) ** (1 / n) - 1


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def r1(x, d=1):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, d)


def pct_rank(values, higher_better=True):
    """None은 중립(0.5). 반환: 0~1"""
    idx = [i for i, v in enumerate(values) if v is not None]
    out = [0.5] * len(values)
    if len(idx) <= 1:
        return out
    srt = sorted(idx, key=lambda i: values[i])
    n = len(srt)
    for rank, i in enumerate(srt):
        p = rank / (n - 1)
        out[i] = p if higher_better else 1 - p
    return out


def main():
    if not DART_KEY:
        sys.exit("[중단] DART_API_KEY가 없습니다. 저장소 Settings → Secrets에 OpenDART 인증키를 등록하세요.")
    Y = NOW.year - 1 if NOW.month >= 4 else NOW.year - 2
    years = list(range(Y - 5, Y + 1))
    log(f"기준 사업연도 {Y} (분석 구간 {years[0]}~{Y})")

    # 1) 주가
    bas_dt, rows = fetch_prices()
    markets = {"KOSPI", "KOSDAQ"} if CFG.get("include_kosdaq", True) else {"KOSPI"}
    prices = {}
    for it in rows:
        if it.get("mrktCtg") not in ("KOSPI", "KOSDAQ"):
            continue
        code = it.get("srtnCd", "").lstrip("A")
        pr, mv = num(it.get("clpr")), num(it.get("mrktTotAmt"))
        sh = num(it.get("lstgStCnt")) or (round(mv / pr) if mv and pr else None)
        prices[code] = [it.get("itmsNm"), pr, r1((mv or 0) / 1e8, 0), it.get("mrktCtg"), sh]
    write_json(DATA / "prices.json", {"date": bas_dt, "p": prices})

    min_mcap = CFG["min_mcap_eok"]
    uni = {c: v for c, v in prices.items()
           if v[3] in markets and c.endswith("0") and "스팩" not in (v[0] or "") and (v[2] or 0) >= min_mcap and v[1]}
    log(f"유니버스(보통주·시총 {min_mcap:,}억 이상): {len(uni)}종목")

    # 2) 고유번호
    cmap = corp_codes()
    uni = {c: v for c, v in uni.items() if c in cmap}
    corps = [cmap[c] for c in uni]

    # 3) 주요계정 6개년
    fa = multi_accounts(corps, Y, "11011")
    fb = multi_accounts(corps, Y - 3, "11011")

    universe_out, stage1 = {}, []
    for code, (name, price, mcap, mkt, shares_now) in uni.items():
        corp = cmap[code]
        rev = series(fa.get(corp), fb.get(corp), "rev")
        op = series(fa.get(corp), fb.get(corp), "op")
        ni = series(fa.get(corp), fb.get(corp), "ni")
        eq = series(fa.get(corp), fb.get(corp), "equity")
        liab = series(fa.get(corp), fb.get(corp), "liab")
        fails = []
        n_prof = CFG["min_profit_years"]
        recent_op = op[-n_prof:]
        if any(v is None for v in recent_op[-3:]):
            fails.append("재무 데이터 부족")
        elif any(v is not None and v <= 0 for v in recent_op):
            fails.append(f"최근 {n_prof}년 중 영업적자 있음")
        if ni[-1] is not None and ni[-1] <= 0:
            fails.append("최근 연도 순손실")
        roes = []
        for k in (3, 4, 5):
            if ni[k] is not None and eq[k]:
                base = mean([eq[k], eq[k - 1]]) if eq[k - 1] else eq[k]
                if base and base > 0:
                    roes.append(ni[k] / base * 100)
        roe3 = mean(roes)
        if roe3 is not None and roe3 < CFG["min_roe_3y"]:
            fails.append(f"3년 평균 ROE {roe3:.1f}% (기준 {CFG['min_roe_3y']}%)")
        rec = dict(code=code, corp=corp, name=name, market=mkt, price=price, mcap=mcap, shares_now=shares_now,
                   rev_ofs=(fa.get(corp) or {}).get("rev_ofs"),
                   rev=rev, op=op, ni=ni, eq=eq, liab=liab, roe3=roe3, fs=(fa.get(corp) or {}).get("fs"))
        universe_out[code] = {"n": name, "m": mkt, "f": fails}
        if not fails:
            stage1.append(rec)
    log(f"1차(이익·ROE) 통과: {len(stage1)}종목")

    # 4) 배당·업종
    dcache, ccache, scache = load_cache("div"), load_cache("company"), load_cache("shares")
    stage2 = []
    for i, rec in enumerate(stage1, 1):
        da = dividends(rec["corp"], Y, dcache)
        db = dividends(rec["corp"], Y - 3, dcache)
        info = company(rec["corp"], ccache)
        if i % 50 == 0:
            save_cache("div", dcache); save_cache("company", ccache); save_cache("shares", scache)
            log(f"  배당·업종 {i}/{len(stage1)}")
        def s6(k):
            return [db[k][2], db[k][1], db[k][0], da[k][2], da[k][1], da[k][0]]
        rec.update(dps=s6("dps"), yld=s6("yld"), payout=s6("payout"), eps=s6("eps"), divtot=s6("total"))
        # 주식수 변동 보정: 현재 상장주식수 ÷ 각 사업보고서 기말 발행주식수
        sa, sb = shares_total(rec["corp"], Y, scache), shares_total(rec["corp"], Y - 3, scache)
        now_sh = rec.get("shares_now")
        ra = now_sh / sa if now_sh and sa else None
        rb = now_sh / sb if now_sh and sb else None
        split = lambda r: r if r and (r >= 1.4 or r <= 0.7) else None  # 액면분할·병합·무상증자 수준의 변동
        ka, kb = split(ra), split(rb)
        if ka or kb:
            for key in ("dps", "eps"):
                v = rec[key]
                rec[key] = [(x / kb if kb and x is not None else x) for x in v[:3]] + [(x / ka if ka and x is not None else x) for x in v[3:]]
        rec["share_ratio"] = ra
        rec["split_adj"] = ka
        rec["shares_fy"] = sa
        ksic = (info.get("ksic") or "")[:2]
        rec["sector"], rec["sector_name"] = ksic, SECTORS.get(ksic, "기타")
        dps = rec["dps"]
        consec = 0
        for v in reversed(dps):
            if v and v > 0:
                consec += 1
            else:
                break
        cut_list = [{"y": Y - 5 + k, "f": r1(dps[k - 1], 0), "t": r1(dps[k], 0)} for k in range(2, 6) if dps[k] is not None and dps[k - 1] and dps[k] < dps[k - 1] * 0.97]
        cuts = len(cut_list)
        rec["cut_list"] = cut_list
        cur_yield = dps[-1] / rec["price"] * 100 if dps[-1] and rec["price"] else None
        prior = [v for v in dps[2:5] if v]
        dps_exp, exp_why, kind = dps[-1], None, None
        p_last = rec["payout"][-1]
        pp = [v for v in rec["payout"][2:5] if v and 0 < v <= 100]
        avg_pp = mean(pp)
        rec["policy_change"] = bool(p_last and avg_pp and p_last >= avg_pp * 1.8)
        rec["med_prior"] = statistics.median(prior) if len(prior) >= 2 else None
        if dps[-1] and len(prior) >= 2:
            med = rec["med_prior"]
            if dps[-1] > med * 1.6:
                if p_last is not None and p_last <= 50:  # 배당성향이 무리 없는 수준 → 정책적 상향
                    kind = "policy"
                    rec["policy_change"] = True
                else:  # 배당성향 과다 → 일회성 배당으로 보고 경상 수준으로
                    kind = "oneoff"
                    g = cagr(dps[1], dps[4], 3) if dps[1] and dps[4] else 0
                    g = min(max(g or 0, 0), 0.15)
                    dps_exp, exp_why = med * (1 + g), f"{Y}년 배당이 직전 3년 중앙값({med:,.0f}원)의 {dps[-1] / med:.1f}배이고 배당성향 {p_last or 0:.0f}% — 일회성으로 보고 경상 배당 수준으로 환산"
        rec["div_kind"] = kind
        rec["dps_exp"], rec["exp_why"] = dps_exp, exp_why
        rec["exp_yield"] = dps_exp / rec["price"] * 100 if dps_exp and rec["price"] else None
        payout = rec["payout"][-1]
        rec.update(consec=consec, cuts=cuts, cur_yield=cur_yield)
        fails = []
        if consec < CFG["min_consec_div_years"]:
            fails.append(f"연속 배당 {consec}년 (기준 {CFG['min_consec_div_years']}년)")
        if cuts > CFG["max_dps_cuts_5y"]:
            fails.append(f"최근 5년 배당 삭감 {cuts}회")
        if payout is not None and payout > CFG["max_payout"]:
            fails.append(f"배당성향 {payout:.0f}% (상한 {CFG['max_payout']}%)")
        ey = rec["exp_yield"]
        if ey is None or ey < CFG["min_current_yield"]:
            fails.append(f"기대 배당률 {ey or 0:.1f}% (기준 {CFG['min_current_yield']}%)" + (f", 표시 배당률 {cur_yield:.1f}%" if exp_why and cur_yield else ""))
        # 일회성 이익 정상화: 순이익이 영업이익의 1.3배를 넘으면 영업이익 × (1 - 법인세율)을 정상 순이익으로 봄
        fin_ = rec["sector"] in FINANCIAL
        TAX = CFG.get("normal_tax_rate", 22) / 100
        def nn(k):
            o, n = rec["op"][k], rec["ni"][k]
            if fin_ or o is None or n is None or o <= 0:
                return n
            return o * (1 - TAX) if n > o * (1 - TAX) * 1.25 else n
        rec["ni_norm"] = [nn(k) for k in range(6)]
        rec["one_off"] = bool(not fin_ and rec["ni"][-1] and rec["ni_norm"][-1] != rec["ni"][-1])
        if any(rec["ni_norm"][k] != rec["ni"][k] for k in (3, 4, 5)):
            roes = []
            for k in (3, 4, 5):
                n, e0, e1 = rec["ni_norm"][k], rec["eq"][k], rec["eq"][k - 1]
                base = mean([e0, e1]) if e0 and e1 else e0
                if n is not None and base and base > 0:
                    roes.append(n / base * 100)
            rec["roe3_raw"], rec["roe3"] = rec["roe3"], mean(roes)
            if rec["roe3"] is not None and rec["roe3"] < CFG["min_roe_3y"]:
                fails.append(f"일회성 이익 제외 시 3년 평균 ROE {rec['roe3']:.1f}% (기준 {CFG['min_roe_3y']}%)")
        # PER (지배주주 EPS 우선)
        eps = rec["eps"][-1]
        if eps and eps > 0 and rec.get("shares_now"):
            per = rec["price"] / eps  # eps는 위에서 현재 주식수 기준으로 보정됨
            if not rec.get("split_adj") and rec.get("shares_fy"):
                per = rec["mcap"] * 1e8 / (eps * rec["shares_fy"])  # 증자·소각 등 소폭 변동은 시가총액 기준으로
        else:
            per = rec["price"] / eps if eps and eps > 0 else (rec["mcap"] * 1e8 / rec["ni"][-1] if rec["ni"][-1] and rec["ni"][-1] > 0 else None)
        rec["per_raw"], rec["per_norm"] = per, None
        if rec["one_off"] and rec["ni_norm"][-1] and rec["ni_norm"][-1] > 0:
            pn = rec["mcap"] * 1e8 / rec["ni_norm"][-1]
            if per is None or pn > per:  # 점수에는 둘 중 높은(보수적인) 값
                rec["per_norm"], per = pn, pn
        rec["per"] = per
        universe_out[rec["code"]].update(f=fails, s=rec["sector_name"], y=r1(cur_yield, 2), ye=r1(rec["exp_yield"], 2), per=r1(per), dps=r1(dps[-1], 1))
        if rec["per_norm"]:
            universe_out[rec["code"]]["pr"] = r1(rec["per_raw"])
        if not fails:
            stage2.append(rec)
    save_cache("div", dcache); save_cache("company", ccache); save_cache("shares", scache)
    log(f"2차(배당) 통과: {len(stage2)}종목")

    # 감사인: 올해 가장 최근 분·반기보고서의 당기 감사인 → 없으면 직전 보고서 → 사업보고서
    acache = load_cache("auditor2")
    reps = [(y, rc, lb) for (y, rc, lb, _, _) in quarter_candidates()] + [(Y, "11011", "사업보고서")]
    for i, rec in enumerate(stage1, 1):
        a, src = None, None
        for y, rc, lb in reps:
            a = auditor(rec["corp"], y, rc, acache)
            if a:
                src = f"{y}년 {lb}"
                break
        rec["auditor"] = (a or {}).get("cur")
        rec["auditor_prev"] = (a or {}).get("prev")
        rec["auditor_src"] = src
        universe_out[rec["code"]].update(a=rec["auditor"])
        if i % 50 == 0:
            save_cache("auditor2", acache)
            log(f"  감사인 {i}/{len(stage1)}")
    save_cache("auditor2", acache)
    log(f"감사인 확인: {sum(1 for r in stage1 if r['auditor'])}/{len(stage1)}종목")

    # 업종 중앙 PER (1차 통과 기업 기준)
    sec_pers = {}
    for rec in stage1:
        if rec.get("per") and 0 < rec["per"] < 100:
            sec_pers.setdefault(rec["sector"], []).append(rec["per"])
    all_pers = [p for v in sec_pers.values() for p in v]
    mkt_med = statistics.median(all_pers) if all_pers else None
    sec_med = {k: statistics.median(v) for k, v in sec_pers.items() if len(v) >= 3}

    # 5) 최근 분기 주요계정 + 전체 재무제표(현금흐름·지배주주·현금·차입금)
    qcands = [(y, rc, lb.replace("보고서", ""), n, m) for (y, rc, lb, n, m) in quarter_candidates()]
    qsets = [(qc, multi_accounts([r["corp"] for r in stage2], qc[0], qc[1])) for qc in qcands] if stage2 else []
    q = qcands[0] if qcands else None
    fcache = load_cache("fsall")
    for i, rec in enumerate(stage2, 1):
        rec["fa"] = fsall(rec["corp"], Y, "11011", fcache) or {}
        qc, qrec = None, None
        for c_, data in qsets:
            if data.get(rec["corp"], {}).get("op"):
                qc, qrec = c_, data[rec["corp"]]
                break
        rec["qc"], rec["qrec"] = qc, qrec
        rec["fq"] = (fsall(rec["corp"], qc[0], qc[1], fcache) or {}) if qc else {}
        if i % 20 == 0:
            save_cache("fsall", fcache)
            log(f"  재무제표 {i}/{len(stage2)}")
    save_cache("fsall", fcache)

    # 6) 올해 결정된 배당, 상장 자회사 지분가치
    qdcache, ncache = load_cache("divq"), load_cache("invest")
    name_map = {norm_name(v[0]): (c, v) for c, v in prices.items() if c.endswith("0")}
    for i, rec in enumerate(stage2, 1):
        rec["divq"] = None if rec.get("split_adj") else div_progress(rec, qcands, qdcache)
        rec["nav"] = nav_value(rec, Y, ncache, name_map)
        if i % 30 == 0:
            save_cache("divq", qdcache); save_cache("invest", ncache)
    save_cache("divq", qdcache); save_cache("invest", ncache)

    # 6-2) 잠정실적, 컨센서스, 거래대금, 우선주
    pcache, sfcache = load_cache("prelim"), load_cache("pref")
    for i, rec in enumerate(stage2, 1):
        try:
            prelim_update(rec, pcache)
        except Exception as e:  # noqa: BLE001
            log(f"  잠정실적 확인 실패 {rec['name']}: {e}")
        cy = rec["qc"][0] if rec.get("qc") else NOW.year
        rec["cons"] = naver_consensus(rec["code"], cy)
        rec["tv20"] = trading_value(rec["code"])
        rec["pref_f"] = pref_factor(rec["corp"], Y, sfcache)
        time.sleep(0.15)
        if i % 20 == 0:
            save_cache("prelim", pcache); save_cache("pref", sfcache)
            log(f"  잠정실적·컨센서스·거래대금 {i}/{len(stage2)}")
    save_cache("prelim", pcache); save_cache("pref", sfcache)
    log(f"  컨센서스 있는 종목 {sum(1 for r in stage2 if r.get('cons'))}/{len(stage2)}, 잠정실적 반영 {sum(1 for r in stage2 if r.get('prelim'))}")

    bgn = (NOW - dt.timedelta(days=CFG.get("disclosure_days", 120))).strftime("%Y%m%d")
    end = NOW.strftime("%Y%m%d")
    TAX = CFG.get("normal_tax_rate", 22) / 100

    # 7) 지표
    for rec in stage2:
        rev, op, ni, eq, liab, eps, dps, yld = (rec[k] for k in ("rev", "op", "ni", "eq", "liab", "eps", "dps", "yld"))
        fa, fq, qc, qrec = rec["fa"], rec["fq"], rec["qc"], rec["qrec"] or {}
        ofs = (rec.get("rev_ofs") or [None])[0]
        rec["sub_ratio"] = ofs / rev[-1] if ofs and rev[-1] and rev[-1] > 0 else None
        rec["sub_heavy"] = bool(rec["sub_ratio"] is not None and rec["sub_ratio"] < 0.2)
        rec["fin_like"] = rec["sector"] not in FINANCIAL and bool(fa.get("fin_like") or fq.get("fin_like"))
        fin = rec["sector"] in FINANCIAL or rec["fin_like"]
        rec["fin"] = fin

        # 지배주주 기준
        cni = (fa.get("ctrl_ni") or [None])[0]
        rec["ctrl_ratio"] = min(cni / ni[-1], 1.0) if cni and ni[-1] and ni[-1] > 0 and cni > 0 else None
        cr = rec["ctrl_ratio"] or 1.0
        qni, qop = qrec.get("ni"), qrec.get("op")
        qcn = fq.get("ctrl_ni")
        ttm = None
        if cni and qcn and qcn[0] is not None and qcn[1] is not None and not rec.get("prelim"):
            ttm = cni + qcn[0] - qcn[1]
        elif qni and qni[0] is not None and qni[1] is not None and ni[-1] is not None:
            ttm = (ni[-1] + qni[0] - qni[1]) * cr
        op_ttm = op[-1] + qop[0] - qop[1] if qop and qop[0] is not None and qop[1] is not None and op[-1] is not None else None
        rec["ttm_ni"], rec["ttm_op"] = ttm, op_ttm
        rec["per_fy"] = rec["per"]
        rec["per_basis"] = f"{Y}년"
        rec["per_ttm"] = rec["mcap"] * 1e8 / ttm if ttm and ttm > 0 else None
        rec["per_norm_ttm"] = None
        if rec["per_ttm"]:
            per = rec["per_ttm"]
            if not fin and op_ttm and op_ttm > 0 and ttm > op_ttm * (1 - TAX) * 1.25 * cr:
                pn = rec["mcap"] * 1e8 / (op_ttm * (1 - TAX) * cr)
                if pn > per:
                    rec["per_norm_ttm"], per = pn, pn
            rec["per"] = per
            rec["per_basis"] = "최근 4개 분기"
        pf = rec.get("pref_f") or 1.0
        if pf > 1.001:
            for k in ("per", "per_fy", "per_ttm", "per_norm_ttm"):
                if rec.get(k):
                    rec[k] = rec[k] * pf
        ceq = fq.get("ctrl_eq") or fa.get("ctrl_eq")
        teq = (qrec.get("equity") or [None])[0] or eq[-1]
        base_eq = ceq if ceq and ceq > 0 else teq
        rec["pbr"] = rec["mcap"] * 1e8 * pf / base_eq if base_eq and base_eq > 0 else None
        rec["bs_src"] = (f"{qc[0]}년 {qc[2]}말" if qc and (fq.get("ctrl_eq") or (qrec.get("equity") or [None])[0]) else f"{Y}년 말") + (" 지배주주 자본" if ceq else " 자본")
        nt = rec["mcap"] * 1e8 / rec["per"] if rec.get("per_norm_ttm") else ttm  # 정상화했으면 정상 순이익으로
        rec["roe_ttm"] = nt / ceq * 100 if nt and ceq and ceq > 0 else None
        qli = (qrec.get("liab") or [None])[0]
        rec["debt"] = None if fin else ((qli / teq * 100) if qli is not None and teq else (liab[-1] / eq[-1] * 100 if liab[-1] is not None and eq[-1] else None))
        src = fq if fq.get("cash") is not None else fa
        rec["net_cash"] = (src["cash"] - (src.get("debt") or 0)) if not fin and src.get("cash") is not None else None
        rec["net_cash_ratio"] = rec["net_cash"] / (rec["mcap"] * 1e8) * 100 if rec["net_cash"] is not None and rec["mcap"] else None
        rec["per_ev"] = None
        if rec["net_cash_ratio"] is not None and rec["net_cash_ratio"] < -50 and op_ttm and op_ttm > 0 and rec.get("per"):
            pe = (rec["mcap"] * 1e8 * pf - rec["net_cash"]) / op_ttm / (1 - TAX)
            if pe > rec["per"]:
                rec["per_ev"], rec["per"] = pe, pe
                rec["per_basis"] = "EV/EBIT 환산(순차입금 반영)"

        rec["rev_cagr"] = cagr(rev[2], rev[5], 3)
        nn_ = rec.get("ni_norm") or ni
        rec["eps_cagr"] = cagr(nn_[2], nn_[5], 3) if (nn_[2] != ni[2] or nn_[5] != ni[5]) else (cagr(eps[2], eps[5], 3) if eps[2] and eps[5] else cagr(ni[2], ni[5], 3))
        rec["dps_cagr"] = cagr(dps[2], dps[5], 3)
        rec["opm"] = mean([op[k] / rev[k] * 100 for k in (3, 4, 5) if op[k] is not None and rev[k]])
        rec["opm_max"] = max([op[k] / rev[k] * 100 for k in range(6) if op[k] is not None and rev[k]] or [0])
        qrv = qrec.get("rev")
        rec["q_margin"] = qop[2] / qrv[2] * 100 if qop and qrv and len(qop) > 2 and qop[2] is not None and qrv[2] else None
        rec["q_nonop"] = qni[2] / qop[2] if qni and qop and len(qni) > 2 and qni[2] is not None and qop[2] and qop[2] > 0 else None
        qd = qop
        rec["q_op_yoy"] = (qd[0] / qd[1] - 1) if qd and qd[0] is not None and qd[1] and qd[1] > 0 else None
        rec["q_label"] = qc[2] if qc else (q[2] if q else None)
        rec["est"] = estimate(rec, qc, qrec, Y, TAX) if qc else None
        est = rec["est"] or {}
        choose_est(rec, rec["est"], rec.get("cons"), TAX)
        if est and rec.get("per_ev") and (est.get("op") or {}).get("base"):
            pe = (rec["mcap"] * 1e8 * pf - rec["net_cash"]) / (est["op"]["base"] * 1e8) / (1 - TAX)
            if est.get("per_fwd") and pe > est["per_fwd"]:
                est["per_fwd"] = r1(pe)
        rec["per_fwd"] = est.get("per_fwd")

        # 업종 비교: 금융·자회사 중심 회사는 제외
        med = sec_med.get(rec["sector"], mkt_med)
        rec["sec_per"] = None if (rec["sub_heavy"] or rec["fin_like"]) else med
        rec["per_rel"] = rec["per"] / rec["sec_per"] if rec["per"] and rec["sec_per"] else None
        # PEG: 금융·경기민감 제외, 올해 이익 감소 시 제외, 성장률 상한 25%
        vs = (est.get("op") or {}).get("vs_last")
        g = min(rec["eps_cagr"], 0.25) if rec["eps_cagr"] else None
        pp = max([x for x in (rec["per"], rec["per_fwd"]) if x] or [0]) or None
        rec["peg"] = pp / (g * 100) if pp and g and g > 0.01 and not fin and rec["sector"] not in CYCLICAL and not (vs is not None and vs < 0) else None

        # 배당: 올해 결정된 배당 → 정책 → 일회성 → 이익 감소 순으로 기대 배당 결정
        eps_fwd = est.get("eps_fwd")
        eps_now = est.get("eps_last_adj")
        dv = rec["divq"]
        p_last = rec["payout"][-1]
        floor = rec.get("med_prior") or 0
        rec["dps_policy"] = None
        if dv:
            ttm_d = dps[-1] + dv["cum"] - dv["prev_same"]
            rec["dps_ttm"] = ttm_d
            if dv.get("cut"):
                rec["dps_exp"] = min(dv["q_last"] * 4, ttm_d)
                rec["exp_why"] = f"배당 삭감 진행 중 — 최근 분기 {dv['q_last']:,.0f}원(직전 분기 평균 {dv['q_prev_avg']:,.0f}원) × 4"
                rec["div_cut_now"] = True
            elif not dv["prev_same"] and dv["cum"] > 0:
                rec["dps_exp"] = max(dps[-1], dv["cum"])
                rec["exp_why"] = (f"올해 처음 중간배당({dv['cum']:,.0f}원) — 연간 배당을 나눠 주는 구조일 수 있어 "
                                  f"작년 연간 {dps[-1]:,.0f}원과 올해 누적 중 큰 값 기준") if rec["dps_exp"] != dps[-1] or ttm_d != dps[-1] else None
            else:
                rec["dps_exp"] = ttm_d
                rec["exp_why"] = None if abs(ttm_d - dps[-1]) < 1 else f"최근 12개월 배당 {ttm_d:,.0f}원 (올해 {dv['label']}까지 {dv['cum']:,.0f}원 + 작년 남은 배당 {dps[-1] - dv['prev_same']:,.0f}원)"
        elif rec["div_kind"] == "policy":
            if eps_fwd and p_last:
                d = eps_fwd * p_last / 100
                rec["dps_policy"] = d
                rec["dps_exp"] = min(max(d, floor), dps[-1])
                if rec["dps_exp"] < dps[-1] * 0.97:
                    rec["exp_why"] = f"배당성향 {p_last:.0f}% 정책 유지 가정 — 올해 추정 EPS {eps_fwd:,.0f}원 기준"
        elif rec["div_kind"] != "oneoff" and eps_fwd and eps_now and eps_fwd < eps_now * 0.85:
            pos = [v for v in rec["payout"][3:] if v and 0 < v <= 100]
            if pos:
                d2 = max(eps_fwd * mean(pos) / 100, min([v for v in dps[2:5] if v] or [0]))
                if d2 < rec["dps_exp"] * 0.9:
                    rec["dps_exp"] = d2
                    rec["exp_why"] = f"올해 추정 EPS({eps_fwd:,.0f}원)가 작년보다 {(1 - eps_fwd / eps_now) * 100:.0f}% 적어 평균 배당성향 {mean(pos):.0f}%를 적용"
        rec["exp_yield"] = rec["dps_exp"] / rec["price"] * 100 if rec["dps_exp"] and rec["price"] else None
        if rec["dps_exp"] and eps_fwd and eps_fwd > 0:
            est["payout_fwd"] = r1(rec["dps_exp"] / eps_fwd * 100, 0)

        # 배당률 밴드: 배당정책이 바뀌었으면 과거 배당률을 현재 배당성향 기준으로 환산
        hist = []
        for k in range(1, 6):
            v = yld[k]
            if not v:
                continue
            if rec["policy_change"] and p_last and rec["payout"][k] and 0 < rec["payout"][k] < p_last and k < 5:
                v = v * p_last / rec["payout"][k]
            hist.append(v)
        rec["band_adj"] = bool(rec["policy_change"])
        rec["yld_avg"] = mean(hist) if len(hist) >= 3 else None
        rec["yld_min"] = min(hist) if len(hist) >= 3 else None
        rec["yld_max"] = max(hist) if len(hist) >= 3 else None

        # 현금흐름: 금융업·금융 자회사 중심 회사 제외, 커버리지는 기대 배당 기준
        cf = fa if fa.get("ocf") else None
        rec["ocf_ni"] = rec["fcf_cov"] = rec["fcf"] = None
        if cf and not fin:
            ocf = cf["ocf"]; capex = cf.get("capex") or [0, 0, 0]
            fcf = [(o - (c or 0)) if o is not None else None for o, c in zip(ocf, capex)]
            rec["fcf"] = list(reversed(fcf))
            ni3 = sum(v for v in ni[3:] if v)
            ocf3 = sum(v for v in ocf if v)
            rec["ocf_ni"] = ocf3 / ni3 if ni3 > 0 else None
            fv = [v for v in fcf if v is not None]
            div_total = (rec["dps_exp"] or 0) * (rec.get("shares_now") or 0)
            rec["fcf_cov"] = mean(fv) / div_total if fv and div_total > 0 else None
        p = p_last
        rec["payout_fit"] = None if p is None else (1.0 if 20 <= p <= 60 else max(0.0, 1 - min(abs(p - 20), abs(p - 60)) / 30))

        # 분리과세 요건 (설정 기준)
        st = CFG.get("sep_tax") or {}
        dt_ = rec["divtot"]
        growth = (dt_[-1] / dt_[-2] - 1) * 100 if dt_[-1] and dt_[-2] else None
        rec["sep_tax"] = bool(p_last and st and (p_last >= st.get("payout_min", 40) or (p_last >= st.get("alt_payout_min", 25) and growth is not None and growth >= st.get("alt_growth_min", 10))))

    kept = []
    for rec in stage2:
        rec["yld_ratio"] = rec["exp_yield"] / rec["yld_avg"] if rec.get("yld_avg") and rec.get("exp_yield") else None
        if rec.get("tv20") is not None and rec["tv20"] < CFG.get("min_trading_value_eok", 1):
            universe_out[rec["code"]].update(f=[f"거래 적음: 20일 평균 거래대금 {rec['tv20']:.1f}억 (기준 {CFG.get('min_trading_value_eok', 1)}억)"])
        elif rec["exp_yield"] is None or rec["exp_yield"] < CFG["min_current_yield"]:
            msg = f"기대 배당률 {rec['exp_yield'] or 0:.1f}% (기준 {CFG['min_current_yield']}%), 표시 배당률 {rec['cur_yield'] or 0:.1f}%"
            if rec.get("exp_why"):
                msg += f" — {rec['exp_why']}"
            universe_out[rec["code"]].update(f=[msg], ye=r1(rec["exp_yield"], 2))
        else:
            universe_out[rec["code"]].update(ye=r1(rec["exp_yield"], 2), per=r1(rec["per"]))
            kept.append(rec)
    if len(kept) < len(stage2):
        log(f"기대 배당률 재판정으로 {len(stage2) - len(kept)}종목 제외")
    stage2 = kept

    # 8) 점수 (가중 평균)
    SPEC = {
        "value": [("per", False, 2), ("per_fwd", False, 1), ("per_rel", False, 1), ("yld_ratio", True, 1), ("peg", False, 1), ("pbr", False, 1)],
        "dividend": [("exp_yield", True, 2), ("consec", True, 1), ("dps_cagr", True, 1), ("payout_fit", None, 1), ("fcf_cov", True, 1)],
        "quality": [("roe3", True, 1), ("roe_ttm", True, 1), ("opm", True, 1), ("ocf_ni", True, 1), ("debt", False, 1), ("net_cash_ratio", True, 1)],
        "growth": [("rev_cagr", True, 1), ("eps_cagr", True, 1), ("q_op_yoy", True, 2), ("g_eff", True, 2)],
    }
    colv = {k: [((r.get("est") or {}).get("g_eff") if k == "g_eff" else r.get(k)) for r in stage2]
            for items in SPEC.values() for k, _, _ in items}
    ranks = {k: (None if hb is None else pct_rank(colv[k], hb)) for items in SPEC.values() for k, hb, _ in items}

    def cats_for(i, over=None):
        out = {}
        for cat, items in SPEC.items():
            tot = ws = 0
            for k, hb, w in items:
                if hb is None:
                    v = colv[k][i] if colv[k][i] is not None else 0.5
                elif over and k in over:
                    vals = list(colv[k]); vals[i] = over[k]
                    v = pct_rank(vals, hb)[i]
                else:
                    v = ranks[k][i]
                tot += v * w; ws += w
            out[cat] = round(100 * tot / ws)
        return out

    def adjust(rec, cats):
        nav = rec.get("nav")
        if nav and rec.get("sub_heavy") and nav["total"] >= rec["mcap"] * 0.5:
            nd = (1 - rec["mcap"] / nav["total"]) * 100
            if nd < 40:
                f = max(0.4, nd / 40) if nd > 0 else 0.4
                cats["value"] = round(cats["value"] * f)
                rec["nav_cut"] = (nd, f)
        return cats

    W = CFG["weights"]; wsum = sum(W.values())
    for i, rec in enumerate(stage2):
        cats = adjust(rec, cats_for(i))
        rec["cats"] = cats
        rec["score"] = round(sum(cats[k] * W[k] for k in W) / wsum, 1)
        rec["fiscal_label"] = f"{Y}년"
        e = rec.get("est") or {}
        if e.get("alt"):
            ca = adjust(rec, cats_for(i, {"per_fwd": e["app"]["per_fwd"], "g_eff": e["app"]["g_eff"]}))
            rec["cats_alt"] = ca
            rec["score_alt"] = round(sum(ca[k] * W[k] for k in W) / wsum, 1)

    # 위험 공시 감점: 상위 후보 + 여유분만 확인
    stage2.sort(key=lambda r: -r["score"])
    risk_bgn = (NOW - dt.timedelta(days=365 * 3)).strftime("%Y%m%d")
    SEVERE = {"상장적격성 실질심사", "횡령·배임", "검찰 고발·통보", "관리종목", "회생절차", "상장폐지 관련", "감사범위 제한"}
    pen = CFG.get("risk_penalty", 15)
    for rec in stage2[: CFG["top_n"] + 15]:
        rec["risk"] = risk_history(rec["corp"], risk_bgn, end)
        bad = [h["t"] for h in rec["risk"] if h["t"] in SEVERE]
        if bad:
            rec["risk_pen"], rec["risk_pen_why"] = -pen, bad
            rec["score"] = round(rec["score"] - pen, 1)
            if rec.get("score_alt") is not None:
                rec["score_alt"] = round(rec["score_alt"] - pen, 1)

    stage2.sort(key=lambda r: -r["score"])
    for rank, rec in enumerate(stage2, 1):
        universe_out[rec["code"]].update(sc=rec["score"], rk=rank)

    # 9) 상위 후보: 공시·위험 이력·주식수 변동 원인·내부자 지분
    top = stage2[: CFG["top_n"]]
    # 지분으로 연결된 후보 묶기 (예: 다우데이타 → 다우기술 → 키움증권)
    codes = {r["code"]: r for r in top}
    adj = {r["code"]: set() for r in top}
    for r in top:
        for c in (r.get("nav") or {}).get("codes", []):
            if c in codes:
                adj[r["code"]].add(c); adj[c].add(r["code"])
    for r in top:
        seen, stack = set(), [r["code"]]
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c); stack += list(adj[c])
        seen.discard(r["code"])
        r["group"] = [codes[c]["name"] for c in seen]
    # 업종 쏠림
    sec_cnt = {}
    for r in top:
        k = "금융(은행·증권·보험·지주)" if r.get("fin") or r.get("fin_like") else r["sector_name"]
        sec_cnt[k] = sec_cnt.get(k, 0) + 1
    conc = [(k, n) for k, n in sorted(sec_cnt.items(), key=lambda x: -x[1]) if n / max(1, len(top)) > 0.2]
    for rec in top:
        rec["disc"], rec["routine_n"] = recent_disclosures(rec, bgn, end)
        if rec.get("risk") is None:
            rec["risk"] = risk_history(rec["corp"], risk_bgn, end)
        sr = rec.get("share_ratio")
        rec["share_cause"] = share_cause(rec["corp"], end) if sr and abs(sr - 1) >= 0.05 else None
        rec["insider"] = insiders(rec["corp"], rec.get("share_cause"))
        rec["reasons"], rec["cautions"] = explain(rec)
    for rec in stage2[CFG["top_n"]:]:
        rec["reasons"], rec["cautions"] = [], []

    cand = [export(r) for r in top]
    latest = {
        "updated": NOW.strftime("%Y-%m-%d %H:%M"), "price_date": bas_dt, "fiscal_year": Y, "years": years,
        "quarter": q[2] if q else None, "counts": {"universe": len(uni), "stage1": len(stage1), "stage2": len(stage2)},
        "config": CFG, "market_per": r1(mkt_med), "candidates": cand,
        "conc": [{"k": k, "n": n} for k, n in conc],
        "others": [export(r, brief=True) for r in stage2[CFG["top_n"]:]],
    }
    write_json(DATA / "latest.json", latest)
    write_json(DATA / "universe.json", {"date": bas_dt, "fy": Y, "u": universe_out})

    # 월별 스냅샷
    ym = NOW.strftime("%Y-%m")
    hf = HIST / f"{ym}.json"
    if not hf.exists():
        write_json(hf, {"date": bas_dt, "fy": Y, "c": [{"code": r["code"], "name": r["name"], "score": r["score"], "price": r["price"], "yield": r1(r["exp_yield"], 2)} for r in top], "r": {r["code"]: i for i, r in enumerate(stage2, 1)}})
    idx = sorted([f.stem for f in HIST.glob("*.json") if f.stem != "index"], reverse=True)
    write_json(HIST / "index.json", idx)
    log(f"완료: 후보 {len(cand)}종목, DART 호출 {CALLS['dart']}회")


def estimate(rec, qc, qrec, Y, TAX):
    """최근 분기 누적으로 올해 연간 실적 추정.
    ① 계절성 반영 = 전년 연간 × 올해 누적 ÷ 전년 같은 기간 누적
    ② 최근 분기 기준 = 누적 + 최근 분기 × 남은 분기 수
    ③ 단순 연환산 = 누적 × 4 ÷ 분기 수
    적용값은 세 값의 중앙값 (금융업은 ②, 건설업은 ③ 제외)"""
    year, _, label, nq, _ = qc
    fin = rec["sector"] in FINANCIAL or rec.get("fin_like")
    out = {"year": year, "label": label, "nq": nq}
    last = {"rev": rec["rev"][-1], "op": rec["op"][-1], "ni": rec["ni"][-1]}
    for k in ("rev", "op", "ni"):
        v = qrec.get(k)
        if not v or v[0] is None:
            continue
        cum, cum_prev = v[0], v[1]
        q_cur = v[2] if len(v) > 2 else None
        q_prev = v[3] if len(v) > 3 else None
        simple = cum * 4 / nq
        seasonal = last[k] * cum / cum_prev if last[k] is not None and cum_prev and cum_prev > 0 and cum > 0 else None
        if k == "ni" and rec.get("one_off"):
            seasonal = None
        runrate = cum + q_cur * (4 - nq) if q_cur is not None and nq < 4 else None
        if fin:
            cands = [x for x in (runrate, simple) if x is not None][:1]
        else:
            cands = [x for x in (seasonal, runrate, None if rec["sector"] in ("41", "42") else simple) if x is not None]
        base = statistics.median(cands) if cands else simple
        e = {
            "cum": r1(cum / 1e8, 0), "cum_prev": r1(cum_prev / 1e8, 0) if cum_prev is not None else None,
            "yoy": r1((cum / cum_prev - 1) * 100) if cum_prev and cum_prev > 0 else None,
            "q": r1(q_cur / 1e8, 0) if q_cur is not None else None,
            "q_yoy": r1((q_cur / q_prev - 1) * 100) if q_cur is not None and q_prev and q_prev > 0 else None,
            "simple": r1(simple / 1e8, 0), "seasonal": r1(seasonal / 1e8, 0) if seasonal is not None else None,
            "runrate": r1(runrate / 1e8, 0) if runrate is not None else None,
            "last": r1(last[k] / 1e8, 0) if last[k] is not None else None,
            "base": r1(base / 1e8, 0), "lo": r1(min(cands) / 1e8, 0) if cands else None, "hi": r1(max(cands) / 1e8, 0) if cands else None,
            "method": "최근 분기 기준(금융업)" if fin else ("세 추정의 중앙값" if len(cands) == 3 else ("①②의 중앙값(건설업은 ③ 제외)" if rec["sector"] in ("41", "42") else "두 추정의 중앙값") if len(cands) == 2 else "단순 연환산"),
        }
        e["uncertain"] = bool(len(cands) >= 2 and min(cands) > 0 and max(cands) / min(cands) > 1.3)
        e["vs_last"] = r1((base / last[k] - 1) * 100) if last[k] and last[k] > 0 else None
        if nq == 2 and last[k] is not None and cum_prev is not None:
            h2 = last[k] - cum_prev
            e["vs_prev_half"] = r1((cum / h2 - 1) * 100) if h2 > 0 else None
            e["prev_half"] = r1(h2 / 1e8, 0)
        out[k] = e
    ni, opn = out.get("ni"), out.get("op")
    if ni:
        nb = ni["base"]
        if not fin and opn and opn["base"] and opn["base"] > 0 and nb > opn["base"] * (1 - TAX) * 1.25:
            ni["norm"] = r1(opn["base"] * (1 - TAX), 0)
            out["one_off"] = True
            nb = ni["norm"]
        # 지배주주 몫: 올해 분기 자료의 비율 → 없으면 작년 비율
        fq = rec.get("fq") or {}
        qcn, qni = fq.get("ctrl_ni"), (qrec.get("ni") or [None])
        cr = (qcn[0] / qni[0]) if qcn and qcn[0] and qni[0] and qni[0] > 0 and qcn[0] > 0 else (rec.get("ctrl_ratio") or 1.0)
        cr = min(cr, 1.0)
        fwd = nb * 1e8 * cr
        out["ctrl_ratio"] = r1(cr * 100, 0)
        sh = rec.get("shares_now")
        if fwd > 0 and sh:
            out["eps_fwd"] = r1(fwd / sh, 0)
            out["per_fwd"] = r1(rec["mcap"] * 1e8 / fwd)
        eps_last = rec["eps"][-1]
        if eps_last and not rec.get("split_adj") and rec.get("shares_fy") and sh:
            eps_last = eps_last * rec["shares_fy"] / sh
        out["eps_last_adj"] = r1(eps_last, 0) if eps_last else None
    return out if len(out) > 3 else None


# 공시 분류: 보고서명 기준 (bad=주의, info=참고, good=긍정, routine=일상 공시로 접음)
DISC_RULES = [
    ("유상증자결정", "유상증자", "bad"), ("전환사채권발행결정", "전환사채", "bad"), ("신주인수권부사채권발행결정", "신주인수권부사채", "bad"),
    ("교환사채권발행결정", "교환사채", "bad"), ("전환청구권행사", "전환 청구", "bad"), ("교환청구권행사", "교환 청구", "bad"),
    ("신주인수권행사", "신주인수권 행사", "bad"), ("최대주주변경", "최대주주 변경", "bad"), ("소송등의제기", "소송", "bad"),
    ("소송등의판결", "소송 판결", "bad"), ("횡령", "횡령·배임", "bad"), ("배임", "횡령·배임", "bad"), ("감자결정", "감자", "bad"),
    ("회사합병결정", "합병", "bad"), ("회사분할합병결정", "분할합병", "bad"), ("회사분할결정", "분할", "bad"),
    ("영업양수결정", "영업양수", "bad"), ("영업양도결정", "영업양도", "bad"), ("영업정지", "영업정지", "bad"),
    ("불성실공시법인", "불성실공시", "bad"), ("관리종목", "관리종목", "bad"), ("공개매수", "공개매수", "bad"),
    ("자기주식처분결정", "자사주 처분", "bad"), ("채무보증", "채무보증", "bad"), ("금전대여", "금전대여", "bad"),
    ("상장적격성", "상장적격성 심사", "bad"), ("투자주의환기", "투자주의환기", "bad"),
    ("유형자산양수결정", "자산 매입", "info"), ("유형자산양도결정", "자산 매각", "info"),
    ("타법인주식및출자증권양수결정", "타법인 주식 취득", "info"), ("타법인주식및출자증권취득결정", "타법인 주식 취득", "info"),
    ("타법인주식및출자증권양도결정", "타법인 주식 처분", "info"), ("타법인주식및출자증권처분결정", "타법인 주식 처분", "info"),
    ("주식등의대량보유상황보고서(약식)", "대량보유(단순투자)", "info"), ("주식등의대량보유상황보고서(일반)", "대량보유(경영참가 가능)", "info"),
    ("배당정책", "배당정책", "info"), ("주주환원", "주주환원 정책", "info"), ("풍문또는보도에대한해명", "풍문 해명", "info"),
    ("조회공시", "조회공시", "info"), ("현저한시황변동", "조회공시", "info"), ("무상증자결정", "무상증자", "info"),
    ("주식분할결정", "액면분할", "info"), ("주식병합결정", "주식병합", "info"), ("자본준비금감소", "감액배당 재원", "info"),
    ("자기주식취득결정", "자사주 취득", "good"), ("자기주식소각결정", "자사주 소각", "good"), ("주식소각결정", "자사주 소각", "good"),
    ("현금ㆍ현물배당결정", "배당 결정", "good"), ("현금·현물배당결정", "배당 결정", "good"), ("기업가치제고계획", "밸류업 공시", "good"),
    ("영업(잠정)실적", "잠정실적", "info"),
]
ROUTINE = ("투자설명서", "증권신고서(파생결합", "증권발행실적보고서", "일괄신고추가서류", "일괄신고서", "증권신고서(채무증권)")
DISC_AMOUNT = {"자산 매입": "tgastInhDecsn.json", "자산 매각": "tgastTrfDecsn.json",
               "타법인 주식 취득": "otcprStkInvscrInhDecsn.json", "타법인 주식 처분": "otcprStkInvscrTrfDecsn.json"}


def disc_amount(corp, api, rcept_no, bgn, end):
    """금액(억원)과 자산총액 대비 비율. 필드명이 달라도 찾을 수 있게 넓게 탐색"""
    j = dart(api, {"corp_code": corp, "bgn_de": bgn, "end_de": end})
    for it in (j or {}).get("list", []):
        if it.get("rcept_no") != rcept_no:
            continue
        amt = ratio = None
        for k, v in it.items():
            kl = k.lower()
            if ratio is None and (kl.endswith("_vs") or "tast_vs" in kl):
                ratio = num(v)
            elif amt is None and any(x in kl for x in ("inhprc", "trfprc", "inh_prc", "trf_prc")) and "vs" not in kl:
                amt = num(v)
        return {"amt": r1(amt / 1e8, 0) if amt else None, "ratio": r1(ratio, 2) if ratio is not None else None}
    return None


def classify(nm):
    key = nm.replace(" ", "")
    sub = "종속회사" in key or "자회사" in key
    f, g, i = set(), set(), set()
    if "매매거래정지" in key:
        if "중요내용공시" in key:
            i.add("공시 관련 일시 정지")
        else:
            f.add("거래정지")
    for kw, lab, kind in DISC_RULES:
        if kw in key:
            if kind == "bad" and sub:
                lab, kind = "자회사 " + lab, "info"
            (f if kind == "bad" else g if kind == "good" else i).add(lab)
    if "분할합병" in f:
        f.discard("분할"); f.discard("합병")
    if key.startswith("[기재정정]"):
        i.add("정정")
    return f, g, i


def recent_disclosures(rec, bgn, end):
    """최근 공시(최대 40건, 일상 증권발행 공시는 접음)와 분류"""
    j = dart("list.json", {"corp_code": rec["corp"], "bgn_de": bgn, "end_de": end, "page_count": "100"})
    out, routine = [], 0
    for it in (j or {}).get("list", []):
        nm = (it.get("report_nm") or "").strip()
        if any(x in nm.replace(" ", "") for x in ROUTINE):
            routine += 1
            continue
        if len(out) >= 40:
            continue
        f, g, i = classify(nm)
        d = {"d": it.get("rcept_dt"), "t": nm, "no": it.get("rcept_no"), "by": it.get("flr_nm"),
             "f": sorted(f), "g": sorted(g), "i": sorted(i)}
        for lab in list(i):
            if lab in DISC_AMOUNT:
                a = disc_amount(rec["corp"], DISC_AMOUNT[lab], it.get("rcept_no"), bgn, end)
                if a and (a["amt"] or a["ratio"] is not None):
                    d["amt"], d["ratio"] = a["amt"], a["ratio"]
                    if a["ratio"] is not None and a["ratio"] >= CFG.get("big_invest_pct", 10):
                        d["f"] = sorted(set(d["f"]) | {"대규모 투자"})
        out.append(d)
    return out, routine


RISK_KW = [("상장적격성", "상장적격성 실질심사"), ("실질심사", "상장적격성 실질심사"), ("불성실공시법인", "불성실공시법인 지정"),
           ("관리종목", "관리종목"), ("투자주의환기", "투자주의환기종목"), ("감사의견", "감사의견 관련"), ("감사범위제한", "감사범위 제한"),
           ("횡령", "횡령·배임"), ("배임", "횡령·배임"), ("과징금", "과징금"), ("감리", "감리 관련"), ("검찰", "검찰 고발·통보"),
           ("회생절차", "회생절차"), ("상장폐지", "상장폐지 관련")]


def risk_history(corp, bgn, end):
    """최근 3년 거래소 공시 중 위험 신호"""
    hits = {}
    for page in (1, 2, 3):
        j = dart("list.json", {"corp_code": corp, "bgn_de": bgn, "end_de": end, "pblntf_ty": "I", "page_no": str(page), "page_count": "100"})
        items = (j or {}).get("list", [])
        for it in items:
            key = (it.get("report_nm") or "").replace(" ", "")
            labs = {lab for kw, lab in RISK_KW if kw in key}
            if "매매거래정지" in key and "중요내용공시" not in key:
                labs.add("매매거래정지")
            if "감사의견" in key and "적정" in key and "비적정" not in key and "부적정" not in key:
                labs.discard("감사의견 관련")
            for lab in labs:
                y = (it.get("rcept_dt") or "")[:4]
                hits.setdefault(lab, set()).add(y)
        if not j or int(j.get("total_page") or 1) <= page:
            break
    return [{"t": lab, "y": sorted(ys)} for lab, ys in sorted(hits.items())]


def share_cause(corp, end):
    """최근 1년 공시에서 발행주식수 변동 원인 찾기"""
    bgn = (NOW - dt.timedelta(days=400)).strftime("%Y%m%d")
    causes = []
    kw = [("무상증자결정", "무상증자"), ("주식분할결정", "액면분할"), ("주식병합결정", "주식병합"), ("주식소각결정", "자사주 소각"),
          ("자기주식소각결정", "자사주 소각"), ("유상증자결정", "유상증자"), ("감자결정", "감자"), ("전환청구권행사", "전환 청구"), ("교환청구권행사", "교환 청구")]
    for page in (1, 2):
        j = dart("list.json", {"corp_code": corp, "bgn_de": bgn, "end_de": end, "page_no": str(page), "page_count": "100"})
        for it in (j or {}).get("list", []):
            key = (it.get("report_nm") or "").replace(" ", "")
            if "종속회사" in key:
                continue
            for k, lab in kw:
                if k in key and lab not in causes:
                    causes.append(lab)
        if not j or int(j.get("total_page") or 1) <= page:
            break
    return causes


def insiders(corp, causes):
    """최근 6개월 임원·주요주주 소유주식 증감 합계 (무상증자·분할이 있으면 계산 보류)"""
    if causes and any(c in ("무상증자", "액면분할", "주식병합") for c in causes):
        return {"skip": True}
    j = dart("elestock.json", {"corp_code": corp})
    since = (NOW - dt.timedelta(days=183)).strftime("%Y%m%d")
    net = n = 0
    for it in (j or {}).get("list", []):
        d = (it.get("rcept_dt") or "").replace("-", "")
        if d < since:
            continue
        v = num(it.get("sp_stock_lmp_irds_cnt"))
        if v:
            net += v; n += 1
    return {"net": net, "n": n} if n else None


def fmt_pct(x, d=1):
    return f"{x * 100:.{d}f}%"


def explain(r):
    R, C = [], []
    est = r.get("est") or {}
    eo = est.get("op") or {}
    if r.get("yld_ratio") and r["yld_ratio"] >= 1.1:
        R.append((r["yld_ratio"], f"기대 배당률 {r['exp_yield']:.1f}%로 최근 5년 평균 {r['yld_avg']:.1f}%보다 높음 — 배당 기준 역사적 저평가 구간" + (" (배당정책 변경을 반영해 과거 배당률 환산)" if r.get("band_adj") else "")))
    if r["consec"] >= 5:
        inc = "삭감 없이 " if r["cuts"] == 0 else ""
        R.append((0.9 + r["consec"] / 20, f"{r['consec']}년 {'이상 ' if r['consec'] >= 6 else ''}연속 배당, {inc}주당배당금 3년 연평균 {fmt_pct(r['dps_cagr'] or 0)} 증가" if r.get("dps_cagr") else f"{r['consec']}년 연속 배당"))
    if r.get("per_rel") and r["per_rel"] < 0.85:
        R.append((1.5 - r["per_rel"], f"PER {r['per']:.1f}배({r['per_basis']} 기준)로 업종({r['sector_name']}) 중앙값 {r['sec_per']:.1f}배 대비 {fmt_pct(1 - r['per_rel'], 0)} 할인"))
    nav = r.get("nav")
    if nav and r["mcap"] and nav["total"] > r["mcap"] * 0.3:
        disc = 1 - r["mcap"] / nav["total"]
        if disc > 0.2:
            R.append((1 + disc, f"보유 상장사 지분가치 {nav['total']:,.0f}억 대비 시가총액 {fmt_pct(disc, 0)} 할인"))
    if r.get("roe3") and r["roe3"] >= 12:
        R.append((r["roe3"] / 12, f"3년 평균 ROE {r['roe3']:.1f}%, 영업이익률 {r['opm']:.1f}%" if r.get("opm") else f"3년 평균 ROE {r['roe3']:.1f}%"))
    if r.get("eps_cagr") and r["eps_cagr"] >= 0.07 and not (eo.get("vs_last") is not None and eo["vs_last"] < 0):
        R.append((1 + min(r["eps_cagr"], 0.25) * 3, f"순이익(EPS) 3년 연평균 {fmt_pct(r['eps_cagr'])} 성장" + (f", 매출 {fmt_pct(r['rev_cagr'])}" if r.get("rev_cagr") else "")))
    if r.get("peg") and r["peg"] < 1:
        R.append((1.4 - r["peg"] / 2, f"PEG {r['peg']:.2f} — 이익 성장 대비 주가 부담 낮음"))
    if r.get("fcf_cov") and r["fcf_cov"] >= 1.5:
        R.append((1 + min(r["fcf_cov"], 4) / 8, f"평균 잉여현금흐름이 기대 배당총액의 {r['fcf_cov']:.1f}배 — 배당 여력 충분"))
    if r.get("net_cash_ratio") and r["net_cash_ratio"] >= 30:
        R.append((1 + r["net_cash_ratio"] / 200, f"순현금이 시가총액의 {r['net_cash_ratio']:.0f}%"))
    if eo.get("vs_last") is not None and eo["vs_last"] >= 5:
        R.append((1.1 + eo["vs_last"] / 100, f"올해 영업이익 추정 {eo['base']:,.0f}억 — 전년 대비 {eo['vs_last']:+.0f}% ({est.get('src') or est['label'] + ' 누적'} 기준)"))
    elif r.get("q_op_yoy") and r["q_op_yoy"] > 0.05:
        R.append((1 + r["q_op_yoy"], f"{r['q_label']} 누적 영업이익 전년 대비 {fmt_pct(r['q_op_yoy'], 0)} 증가"))
    if r.get("sep_tax"):
        R.append((1.0, f"{r['fiscal_label']} 배당이 분리과세 요건(설정 기준)에 해당"))
    R = [t for _, t in sorted(R, key=lambda x: -x[0])][:4]

    # 위험 이력·감사인은 맨 앞에
    for h in r.get("risk") or []:
        C.append(f"최근 3년 위험 공시: {h['t']} ({', '.join(h['y'])}년) — 원문 확인")
    if r.get("auditor_prev") and r.get("auditor") and norm_name(r["auditor_prev"]) != norm_name(r["auditor"]):
        C.append(f"감사인 교체: {r['auditor_prev']} → {r['auditor']} (지정 여부 확인)")
    if r.get("div_cut_now"):
        C.append(f"올해 분기배당 삭감 — {r['exp_why'].split(' — ', 1)[-1]}")
    elif r.get("exp_why"):
        C.append(f"표시 배당률 {r['cur_yield']:.1f}%는 작년 배당 기준 — 기대 배당률 {r['exp_yield']:.1f}%로 평가 ({r['exp_why']})")
    if r.get("div_kind") == "policy":
        C.append(f"작년 배당 급증은 배당성향 {r['payout'][-1]:.0f}% 수준의 정책적 상향으로 판단 — 배당정책 공시 확인")
    pf = est.get("payout_fwd")
    if pf and pf >= 70 and eo.get("vs_last") is not None and eo["vs_last"] < 0:
        C.append(f"올해 추정 이익 기준 배당성향 {pf:.0f}%에 이익 감소 — 배당 축소 위험")
    elif pf and pf > 90:
        C.append(f"올해 추정 이익 기준 배당성향 {pf:.0f}% — 현 배당 유지 부담")
    sr = r.get("share_ratio")
    cause = ", ".join(r.get("share_cause") or [])
    if r.get("split_adj"):
        C.append(f"발행주식수가 {r['fiscal_label']} 말 대비 {sr:.2f}배" + (f"({cause})" if cause else "") + " — 과거 주당배당금·EPS를 현재 주식수 기준으로 환산")
    elif sr and abs(sr - 1) >= 0.05:
        C.append(f"발행주식수가 {r['fiscal_label']} 말 대비 {(sr - 1) * 100:+.0f}%" + (f" — {cause}" if cause else " — 원인 확인 필요"))
    if r.get("yld_ratio") and r["yld_ratio"] > 1.8:
        C.append("기대 배당률이 평소의 1.8배 이상 — 주가 급락 원인(실적·소송 등) 확인")
    if r["sector"] in CYCLICAL:
        C.append(f"경기민감 업종({r['sector_name']}) — 이익 정점에서 PER이 낮게 보일 수 있음")
    if r["sector"] in FINANCIAL:
        C.append("금융업 — 분기 이익 변동이 커 추정 신뢰도 낮음, 부채비율·현금흐름 지표는 점수에서 제외")
    elif r.get("fin_like"):
        C.append("금융 자회사 중심 연결 — 금융업처럼 처리 (현금흐름·부채비율 제외, 업종 PER 비교 제외)")
    elif r.get("sub_heavy"):
        C.append(f"별도 매출이 연결의 {r['sub_ratio'] * 100:.0f}% — 자회사 중심 회사, 업종 PER 비교 제외")
    if r.get("ctrl_ratio") and r["ctrl_ratio"] < 0.85:
        C.append(f"연결 순이익 중 지배주주 몫 {r['ctrl_ratio'] * 100:.0f}% — PER·PBR은 지배주주 기준으로 계산")
    if any(x in r["name"] for x in ("홀딩스", "지주")) or (nav and r["mcap"] and nav["total"] > r["mcap"] * 0.3):
        C.append("지주회사 성격 — 할인 해소 계기(자사주 소각·밸류업) 확인")
    if r.get("per_norm_ttm"):
        C.append(f"최근 4개 분기 순이익이 세후 영업이익보다 크게 많음 — 일회성 이익으로 보고 정상화 PER {r['per_norm_ttm']:.1f}배로 평가 (순이익 기준 {r['per_ttm']:.1f}배)")
    elif r.get("per_norm") and r.get("per_raw"):
        C.append(f"{r['fiscal_label']} 순이익에 일회성 이익 — 정상화 PER {r['per_norm']:.1f}배 (순이익 기준 {r['per_raw']:.1f}배)")
    if r.get("q_margin") and r.get("opm") and r["opm"] > 0 and r["q_margin"] >= r["opm"] * 1.3:
        top_ = " — 6년 중 최고 수준" if r["q_margin"] > (r.get("opm_max") or 0) else ""
        C.append(f"이익률 정점 가능성: 최근 분기 영업이익률 {r['q_margin']:.1f}% (3년 평균 {r['opm']:.1f}%){top_}")
    if r.get("q_nonop") is not None and r["q_nonop"] < 0.4 and not r.get("fin"):
        C.append(f"최근 분기 순이익이 영업이익의 {r['q_nonop'] * 100:.0f}% — 영업외 손실(평가손실·외환·파생) 확인")
    if eo.get("uncertain"):
        C.append(f"올해 추정 범위가 넓음({eo['lo']:,.0f}~{eo['hi']:,.0f}억) — 다음 분기 실적으로 확인 필요")
    if eo.get("vs_last") is not None and eo["vs_last"] <= -15:
        C.append(f"올해 영업이익 추정치가 전년보다 {abs(eo['vs_last']):.0f}% 적음 ({est['label']} 누적 기준) — 실적 둔화")
    if r.get("ocf_ni") is not None and r["ocf_ni"] < 0.8:
        C.append(f"영업현금흐름이 순이익의 {r['ocf_ni']:.1f}배 — 이익의 현금 전환 약함")
    if r["cuts"]:
        C.append("최근 5년 배당 삭감: " + ", ".join(f"{c['y']}년 {c['f']:,.0f}→{c['t']:,.0f}원" for c in r.get("cut_list", [])))
    ins = r.get("insider")
    if ins and not ins.get("skip") and ins.get("net", 0) < 0:
        C.append(f"최근 6개월 임원·주요주주 순매도 {abs(ins['net']):,.0f}주 ({ins['n']}건 보고)")
    flags = sorted({f for d in r.get("disc") or [] for f in d.get("f", [])})
    if flags:
        C.append("최근 공시: " + ", ".join(flags) + " — 내용 확인")
    if r.get("tv20") is not None and r["tv20"] < CFG.get("low_trading_value_eok", 5):
        C.insert(0, f"거래 적음: 20일 평균 거래대금 {r['tv20']:.1f}억 — 원하는 가격에 사고팔기 어려울 수 있음")
    if r.get("risk_pen"):
        C.insert(0, f"위험 공시로 점수 {r['risk_pen']}점 감점: {', '.join(r['risk_pen_why'])}")
    if r.get("nav_cut"):
        C.append(f"지주사 평가: 보유 상장사 지분가치 대비 할인 {r['nav_cut'][0]:.0f}%로 통상 수준(40%)보다 작아 가치 점수를 {r['nav_cut'][1]:.0%}로 낮춤")
    if r.get("per_ev"):
        C.append(f"순차입금이 시가총액의 {abs(r['net_cash_ratio']):.0f}% — 빚을 포함한 기업가치 기준으로 PER {r['per_ev']:.1f}배 상당으로 평가")
    if r.get("group"):
        C.append(f"같은 이익원: {', '.join(r['group'])}와 지분으로 연결 — 함께 보유하면 같은 실적에 겹쳐 노출")
    if r.get("prelim_fail"):
        C.append(r["prelim_fail"])
    if est.get("src") == "보수 추정" and est.get("alt"):
        C.append(f"올해 추정은 보수적으로 적용(추정 PER {est.get('per_fwd')}배) — 앱 원래 추정은 {est['app']['per_fwd']}배")
    return R, C


def export(r, brief=False):
    base = {
        "code": r["code"], "name": r["name"], "market": r["market"], "sector": r["sector_name"],
        "price": r["price"], "mcap": r["mcap"], "score": r["score"], "cats": r["cats"],
        "yield": r1(r["exp_yield"], 2), "yield_shown": r1(r["cur_yield"], 2), "dps_exp": r1(r.get("dps_exp"), 0), "exp_why": r.get("exp_why"),
        "per": r1(r.get("per")), "per_basis": r.get("per_basis"), "dps": r1(r["dps"][-1], 1),
        "auditor": r.get("auditor"),
    }
    if brief:
        return base
    eok = lambda xs: [r1(v / 1e8, 0) if v is not None else None for v in xs]
    pct100 = lambda x: r1(x * 100) if x is not None else None
    base.update({
        "pbr": r1(r.get("pbr"), 2), "roe3": r1(r.get("roe3")), "roe_ttm": r1(r.get("roe_ttm")), "opm": r1(r.get("opm")), "debt": r1(r.get("debt"), 0),
        "rev_cagr": pct100(r.get("rev_cagr")), "eps_cagr": pct100(r.get("eps_cagr")), "dps_cagr": pct100(r.get("dps_cagr")),
        "q_op_yoy": pct100(r.get("q_op_yoy")), "q_margin": r1(r.get("q_margin")),
        "payout": r1(r["payout"][-1]), "consec": r["consec"], "cuts": r["cuts"], "cut_list": r.get("cut_list", []),
        "yld_avg": r1(r.get("yld_avg"), 2), "yld_min": r1(r.get("yld_min"), 2), "yld_max": r1(r.get("yld_max"), 2), "band_adj": bool(r.get("band_adj")),
        "yld_ratio": r1(r.get("yld_ratio"), 2), "per_rel": r1(r.get("per_rel"), 2), "sec_per": r1(r.get("sec_per")),
        "peg": r1(r.get("peg"), 2), "ocf_ni": r1(r.get("ocf_ni"), 2), "fcf_cov": r1(r.get("fcf_cov"), 1),
        "per_fy": r1(r.get("per_fy")), "per_ttm": r1(r.get("per_ttm")), "per_norm_ttm": r1(r.get("per_norm_ttm")),
        "per_raw": r1(r.get("per_raw")), "per_norm": r1(r.get("per_norm")), "per_fwd": r1(r.get("per_fwd")), "one_off": bool(r.get("one_off")),
        "roe3_raw": r1(r.get("roe3_raw")), "ctrl_ratio": r1((r.get("ctrl_ratio") or 0) * 100, 0) if r.get("ctrl_ratio") else None,
        "net_cash": r1(r["net_cash"] / 1e8, 0) if r.get("net_cash") is not None else None, "net_cash_ratio": r1(r.get("net_cash_ratio"), 0),
        "nav": r.get("nav"), "nav_disc": r1((1 - r["mcap"] / r["nav"]["total"]) * 100, 0) if r.get("nav") and r["nav"]["total"] > r["mcap"] * 0.3 else None,
        "fin": bool(r.get("fin")), "fin_like": bool(r.get("fin_like")), "sub_heavy": bool(r.get("sub_heavy")),
        "dps_policy": r1(r.get("dps_policy"), 0), "div_kind": r.get("div_kind"), "dps_ttm": r1(r.get("dps_ttm"), 0), "div_cut_now": bool(r.get("div_cut_now")),
        "sep_tax": bool(r.get("sep_tax")), "risk": r.get("risk") or [], "auditor_prev": r.get("auditor_prev"),
        "share_cause": r.get("share_cause"), "insider": r.get("insider"), "routine_n": r.get("routine_n", 0),
        "series": {"dps": [r1(x, 1) for x in r["dps"]], "eps": [r1(x, 0) for x in r["eps"]], "yld": r["yld"], "payout": r["payout"],
                   "rev": eok(r["rev"]), "op": eok(r["op"]), "ni": eok(r["ni"]), "fcf": eok(r["fcf"] or [])},
        "reasons": r["reasons"], "cautions": r["cautions"], "fs": (r.get("fa") or {}).get("fs") or r.get("fs"),
        "est": r.get("est"), "disc": r.get("disc", []),
        "auditor_src": r.get("auditor_src"), "bs_src": r.get("bs_src"),
        "share_ratio": r1(r.get("share_ratio"), 3), "split_adj": bool(r.get("split_adj")),
        "payout_flag": bool(r["payout"][-1] and r["payout"][-1] >= CFG["payout_flag"]),
        "tv20": r1(r.get("tv20")), "prelim": r.get("prelim"), "pref_f": r1(r.get("pref_f"), 3), "per_ev": r1(r.get("per_ev")),
        "score_alt": r.get("score_alt"), "cats_alt": r.get("cats_alt"), "group": r.get("group") or [],
        "risk_pen": r.get("risk_pen"),
    })
    return base


if __name__ == "__main__":
    main()
