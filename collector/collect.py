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
import os, io, sys, json, time, math, zipfile, statistics, datetime as dt
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
    name = f"fin_{year}_{reprt}" if reprt == "11011" else f"finq_{year}_{reprt}"
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


def cashflow(corp, year, cache):
    key = f"{corp}_{year}"
    if key in cache:
        return cache[key]
    out = None
    for fs in ("CFS", "OFS"):
        j = dart("fnlttSinglAcntAll.json", {"corp_code": corp, "bsns_year": str(year), "reprt_code": "11011", "fs_div": fs})
        if not j:
            continue
        ocf = capex = None
        for it in j.get("list", []):
            aid = it.get("account_id", "")
            nm = (it.get("account_nm") or "").replace(" ", "")
            if it.get("sj_div") != "CF":
                continue
            vals = [num(it.get("thstrm_amount")), num(it.get("frmtrm_amount")), num(it.get("bfefrmtrm_amount"))]
            if ocf is None and (aid == "ifrs-full_CashFlowsFromUsedInOperatingActivities" or (nm.startswith("영업활동") and "현금흐름" in nm)):
                ocf = vals
            if capex is None and (aid == "ifrs-full_PurchaseOfPropertyPlantAndEquipment" or "유형자산의취득" in nm):
                capex = [abs(v) if v is not None else None for v in vals]
        if ocf:
            out = {"ocf": ocf, "capex": capex or [0, 0, 0]}
            break
    cache[key] = out
    return out


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
        cuts = sum(1 for k in range(2, 6) if dps[k] is not None and dps[k - 1] and dps[k] < dps[k - 1] * 0.97)
        cur_yield = dps[-1] / rec["price"] * 100 if dps[-1] and rec["price"] else None
        prior = [v for v in dps[2:5] if v]
        dps_exp, exp_why = dps[-1], None
        if dps[-1] and len(prior) >= 2:
            med = statistics.median(prior)
            if dps[-1] > med * 1.6:  # 직전 3년 중앙값의 1.6배 초과 → 일회성·정책 변경 배당으로 보고 경상 수준으로
                g = cagr(dps[1], dps[4], 3) if dps[1] and dps[4] else 0
                g = min(max(g or 0, 0), 0.15)
                dps_exp, exp_why = med * (1 + g), f"{Y}년 배당이 직전 3년 중앙값({med:,.0f}원)의 {dps[-1] / med:.1f}배 — 경상 배당 수준으로 환산"
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
            return o * (1 - TAX) if n > o * 1.3 else n
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

    # 5) 현금흐름·최근 분기 (후보만)
    fcache = load_cache("cashflow")
    for i, rec in enumerate(stage2, 1):
        rec["cf"] = cashflow(rec["corp"], Y, fcache)
        if i % 30 == 0:
            save_cache("cashflow", fcache)
    save_cache("cashflow", fcache)

    qcands = [(y, rc, lb.replace("보고서", ""), n, m) for (y, rc, lb, n, m) in quarter_candidates()]
    qsets = [(qc, multi_accounts([r["corp"] for r in stage2], qc[0], qc[1])) for qc in qcands] if stage2 else []
    q = qcands[0] if qcands else None

    # 7) 최근 공시 (후보 상위만)
    bgn = (NOW - dt.timedelta(days=CFG.get("disclosure_days", 120))).strftime("%Y%m%d")
    end = NOW.strftime("%Y%m%d")

    # 6) 지표·점수
    for rec in stage2:
        rev, op, ni, eq, liab, eps, dps, yld = (rec[k] for k in ("rev", "op", "ni", "eq", "liab", "eps", "dps", "yld"))
        fin = rec["sector"] in FINANCIAL
        rec["pbr"] = rec["mcap"] * 1e8 / eq[-1] if eq[-1] and eq[-1] > 0 else None
        rec["bs_src"] = f"{Y}년 말"
        rec["rev_cagr"] = cagr(rev[2], rev[5], 3)
        nn_ = rec.get("ni_norm") or ni
        if nn_[2] != ni[2] or nn_[5] != ni[5]:  # 일회성 이익이 낀 해가 있으면 정상 순이익으로 성장률 계산
            rec["eps_cagr"] = cagr(nn_[2], nn_[5], 3)
        else:
            rec["eps_cagr"] = cagr(eps[2], eps[5], 3) if eps[2] and eps[5] else cagr(ni[2], ni[5], 3)
        rec["dps_cagr"] = cagr(dps[2], dps[5], 3)
        rec["opm"] = mean([op[k] / rev[k] * 100 for k in (3, 4, 5) if op[k] is not None and rev[k]])
        rec["debt"] = None if fin else (liab[-1] / eq[-1] * 100 if liab[-1] is not None and eq[-1] else None)
        hist = [v for v in yld[1:] if v]
        rec["yld_avg"] = mean(hist) if len(hist) >= 3 else None
        rec["yld_min"] = min(hist) if len(hist) >= 3 else None
        rec["yld_max"] = max(hist) if len(hist) >= 3 else None
        rec["yld_ratio"] = None  # 기대 배당률 확정 후 아래에서 계산
        med = sec_med.get(rec["sector"], mkt_med)
        rec["sec_per"] = med
        rec["per_rel"] = rec["per"] / med if rec["per"] and med else None
        rec["peg"] = rec["per"] / (rec["eps_cagr"] * 100) if rec["per"] and rec["eps_cagr"] and rec["eps_cagr"] > 0.01 else None
        cf = rec.get("cf")
        rec["ocf_ni"] = rec["fcf_cov"] = None
        rec["fcf"] = None
        if cf and not fin:
            ocf = cf["ocf"]; capex = cf["capex"]
            fcf = [(o - (c or 0)) if o is not None else None for o, c in zip(ocf, capex)]
            rec["fcf"] = list(reversed(fcf))  # [Y-2, Y-1, Y]
            ni3 = sum(v for v in ni[3:] if v)
            ocf3 = sum(v for v in ocf if v)
            rec["ocf_ni"] = ocf3 / ni3 if ni3 > 0 else None
            div3 = sum((v or 0) * 1e6 for v in rec["divtot"][3:])
            fcf3 = sum(v for v in fcf if v is not None)
            rec["fcf_cov"] = fcf3 / div3 if div3 > 0 else None
        qc, qrec = None, None
        for c_, data in qsets:
            if data.get(rec["corp"], {}).get("op"):
                qc, qrec = c_, data[rec["corp"]]
                break
        qd = (qrec or {}).get("op")
        rec["q_op_yoy"] = (qd[0] / qd[1] - 1) if qd and qd[0] is not None and qd[1] and qd[1] > 0 else None
        rec["q_label"] = qc[2] if qc else (q[2] if q else None)
        rec["est"] = estimate(rec, qc, qrec, Y) if qc else None
        qeq = ((qrec or {}).get("equity") or [None])[0]
        qli = ((qrec or {}).get("liab") or [None])[0]
        if qeq and qeq > 0:
            rec["pbr"] = rec["mcap"] * 1e8 / qeq
            rec["bs_src"] = f"{qc[0]}년 {qc[2]}말"
            if not fin and qli is not None:
                rec["debt"] = qli / qeq * 100
        if rec["est"] and rec["est"].get("per_fwd"):
            rec["per_fwd"] = rec["est"]["per_fwd"]
        eps_fwd = (rec.get("est") or {}).get("eps_fwd")
        eps_now = (rec.get("est") or {}).get("eps_last_adj")
        pos = [v for v in rec["payout"][3:] if v and 0 < v <= 100]
        if eps_fwd and eps_now and eps_fwd < eps_now * 0.85 and pos:
            d2 = eps_fwd * mean(pos) / 100
            floor = min([v for v in rec["dps"][2:5] if v] or [0])  # 최근 3년 중 가장 낮았던 배당 밑으로는 내리지 않음
            d2 = max(d2, floor)
            if d2 < rec["dps_exp"] * 0.9:
                rec["dps_exp"] = d2
                rec["exp_why"] = (rec["exp_why"] + " / " if rec["exp_why"] else "") + f"올해 추정 EPS({eps_fwd:,.0f}원)가 작년보다 {(1 - eps_fwd / eps_now) * 100:.0f}% 적어 평균 배당성향 {mean(pos):.0f}%를 적용"
                rec["exp_yield"] = d2 / rec["price"] * 100
        p = rec["payout"][-1]
        rec["payout_fit"] = None if p is None else (1.0 if 20 <= p <= 60 else max(0.0, 1 - min(abs(p - 20), abs(p - 60)) / 30))

    kept = []
    for rec in stage2:
        rec["yld_ratio"] = rec["exp_yield"] / rec["yld_avg"] if rec.get("yld_avg") and rec.get("exp_yield") else None
        if rec["exp_yield"] is None or rec["exp_yield"] < CFG["min_current_yield"]:
            msg = f"기대 배당률 {rec['exp_yield'] or 0:.1f}% (기준 {CFG['min_current_yield']}%), 표시 배당률 {rec['cur_yield'] or 0:.1f}%"
            universe_out[rec["code"]].update(f=[msg], ye=r1(rec["exp_yield"], 2))
        else:
            universe_out[rec["code"]]["ye"] = r1(rec["exp_yield"], 2)
            kept.append(rec)
    if len(kept) < len(stage2):
        log(f"기대 배당률 재판정으로 {len(stage2) - len(kept)}종목 제외")
    stage2 = kept

    def col(k):
        return [r.get(k) for r in stage2]
    parts = {
        "value": [pct_rank(col("per_fwd"), False), pct_rank(col("per"), False), pct_rank(col("per_rel"), False), pct_rank(col("yld_ratio")), pct_rank(col("peg"), False), pct_rank(col("pbr"), False)],
        "dividend": [pct_rank(col("exp_yield")), pct_rank(col("consec")), pct_rank(col("dps_cagr")), [x if x is not None else 0.5 for x in col("payout_fit")], pct_rank(col("fcf_cov"))],
        "quality": [pct_rank(col("roe3")), pct_rank(col("opm")), pct_rank(col("ocf_ni")), pct_rank(col("debt"), False)],
        "growth": [pct_rank(col("rev_cagr")), pct_rank(col("eps_cagr")), pct_rank(col("q_op_yoy")), pct_rank([((r.get("est") or {}).get("op") or {}).get("vs_last") for r in stage2])],
    }
    W = CFG["weights"]; wsum = sum(W.values())
    for i, rec in enumerate(stage2):
        cats = {k: round(100 * sum(v[i] for v in vs) / len(vs)) for k, vs in parts.items()}
        rec["cats"] = cats
        rec["score"] = round(sum(cats[k] * W[k] for k in W) / wsum, 1)
        rec["fiscal_label"] = f"{Y}년"
        rec["reasons"], rec["cautions"] = explain(rec)

    stage2.sort(key=lambda r: -r["score"])
    for rank, rec in enumerate(stage2, 1):
        universe_out[rec["code"]].update(sc=rec["score"], rk=rank)

    top = stage2[: CFG["top_n"]]
    for rec in top:
        rec["disc"] = recent_disclosures(rec["corp"], bgn, end)
        flags = sorted({f for d in rec["disc"] for f in d.get("f", [])})
        rec["disc_flags"] = flags
        if flags:
            rec["cautions"].append("최근 공시: " + ", ".join(flags) + " — 내용 확인")
    cand = [export(r) for r in top]
    latest = {
        "updated": NOW.strftime("%Y-%m-%d %H:%M"), "price_date": bas_dt, "fiscal_year": Y, "years": years,
        "quarter": q[2] if q else None, "counts": {"universe": len(uni), "stage1": len(stage1), "stage2": len(stage2)},
        "config": CFG, "market_per": r1(mkt_med), "candidates": cand,
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


def estimate(rec, qc, qrec, Y):
    """최근 분기 누적으로 올해 연간 실적 추정.
    단순 연환산 = 누적 × 4/분기수, 추정(계절성 반영) = 전년 연간 × 올해 누적 / 전년 같은 기간 누적"""
    year, _, label, nq, _ = qc
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
            seasonal = None  # 작년 순이익에 일회성 이익이 있어 계절성 비율이 왜곡됨
        e = {
            "cum": r1(cum / 1e8, 0), "cum_prev": r1(cum_prev / 1e8, 0) if cum_prev is not None else None,
            "yoy": r1((cum / cum_prev - 1) * 100) if cum_prev and cum_prev > 0 else None,
            "q": r1(q_cur / 1e8, 0) if q_cur is not None else None,
            "q_yoy": r1((q_cur / q_prev - 1) * 100) if q_cur is not None and q_prev and q_prev > 0 else None,
            "simple": r1(simple / 1e8, 0), "seasonal": r1(seasonal / 1e8, 0) if seasonal is not None else None,
            "last": r1(last[k] / 1e8, 0) if last[k] is not None else None,
        }
        runrate = cum + q_cur * (4 - nq) if q_cur is not None and nq < 4 else None
        e["runrate"] = r1(runrate / 1e8, 0) if runrate is not None else None
        cands = [x for x in (seasonal, runrate) if x is not None]
        base = min(cands) if cands else simple  # 보수적으로 낮은 값
        e["base"] = r1(base / 1e8, 0)
        e["method"] = "최근 분기 기준" if runrate is not None and base == runrate and (seasonal is None or runrate < seasonal) else ("계절성 반영" if seasonal is not None and base == seasonal else "단순 연환산")
        e["vs_last"] = r1((base / last[k] - 1) * 100) if last[k] and last[k] > 0 else None
        if nq == 2 and last[k] is not None and cum_prev is not None:  # 반기: 직전 반기(전년 하반기) 대비
            h2 = last[k] - cum_prev
            e["vs_prev_half"] = r1((cum / h2 - 1) * 100) if h2 > 0 else None
            e["prev_half"] = r1(h2 / 1e8, 0)
        out[k] = e
    ni = out.get("ni")
    TAX = CFG.get("normal_tax_rate", 22) / 100
    if ni and out.get("op") and rec["sector"] not in FINANCIAL:
        ob = out["op"]["base"]
        nb = ni["base"]
        if ob and ob > 0 and nb and nb > ob * 1.3:  # 올해도 일회성 이익이면 영업이익 기준으로
            ni["norm"] = r1(ob * (1 - TAX), 0)
            out["one_off"] = True
    eps_last, ni_last = rec["eps"][-1], last["ni"]
    if eps_last and not rec.get("split_adj") and rec.get("shares_fy") and rec.get("shares_now"):
        eps_last = eps_last * rec["shares_fy"] / rec["shares_now"]  # 증자·소각 반영
    if ni and eps_last and ni_last and ni_last > 0:
        base = (ni.get("norm") or ni["base"]) * 1e8
        eps_fwd = eps_last * base / ni_last
        out["eps_fwd"] = r1(eps_fwd, 0)
        out["eps_last_adj"] = r1(eps_last, 0)
        out["per_fwd"] = r1(rec["price"] / eps_fwd) if eps_fwd > 0 else None
        dd = rec.get("dps_exp") or rec["dps"][-1]
        if dd and eps_fwd > 0:
            out["payout_fwd"] = r1(dd / eps_fwd * 100, 0)
    return out if len(out) > 3 else None


# 공시 분류: 보고서명 기준 (bad=주의, info=참고, good=긍정)
DISC_RULES = [
    ("유상증자결정", "유상증자", "bad"), ("전환사채권발행결정", "전환사채", "bad"), ("신주인수권부사채권발행결정", "신주인수권부사채", "bad"),
    ("교환사채권발행결정", "교환사채", "bad"), ("최대주주변경", "최대주주 변경", "bad"), ("소송등의제기", "소송", "bad"),
    ("소송등의판결", "소송 판결", "bad"), ("횡령", "횡령·배임", "bad"), ("배임", "횡령·배임", "bad"), ("감자결정", "감자", "bad"),
    ("회사합병결정", "합병", "bad"), ("회사분할합병결정", "분할합병", "bad"), ("회사분할결정", "분할", "bad"),
    ("영업양수결정", "영업양수", "bad"), ("영업양도결정", "영업양도", "bad"), ("영업정지", "영업정지", "bad"),
    ("불성실공시법인", "불성실공시", "bad"), ("관리종목", "관리종목", "bad"), ("매매거래정지", "거래정지", "bad"),
    ("공개매수", "공개매수", "bad"), ("자기주식처분결정", "자사주 처분", "bad"),
    ("유형자산양수결정", "자산 매입", "info"), ("유형자산양도결정", "자산 매각", "info"),
    ("타법인주식및출자증권양수결정", "타법인 주식 취득", "info"), ("타법인주식및출자증권양도결정", "타법인 주식 처분", "info"),
    ("주식등의대량보유상황보고서", "대량보유 변동", "info"), ("배당정책", "배당정책", "info"), ("주주환원", "주주환원 정책", "info"),
    ("자기주식취득결정", "자사주 취득", "good"), ("자기주식소각결정", "자사주 소각", "good"), ("주식소각결정", "자사주 소각", "good"),
    ("현금ㆍ현물배당결정", "배당 결정", "good"), ("현금·현물배당결정", "배당 결정", "good"), ("기업가치제고계획", "밸류업 공시", "good"),
    ("영업(잠정)실적", "잠정실적", "info"),
]
# 금액을 붙일 주요사항보고서 (OpenDART 주요사항보고서 API)
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


def recent_disclosures(corp, bgn, end):
    """최근 공시 목록(최대 40건)과 분류 표시"""
    j = dart("list.json", {"corp_code": corp, "bgn_de": bgn, "end_de": end, "page_count": "100"})
    out = []
    for it in (j or {}).get("list", [])[:40]:
        nm = (it.get("report_nm") or "").strip()
        key = nm.replace(" ", "")
        sub = "종속회사" in key or "자회사" in key
        f, g, i = set(), set(), set()
        for kw, lab, kind in DISC_RULES:
            if kw in key:
                if kind == "bad" and sub:
                    lab, kind = "자회사 " + lab, "info"
                (f if kind == "bad" else g if kind == "good" else i).add(lab)
        if "분할합병" in f:
            f.discard("분할"); f.discard("합병")
        if key.startswith("[기재정정]"):
            i.add("정정")
        d = {"d": it.get("rcept_dt"), "t": nm, "no": it.get("rcept_no"), "by": it.get("flr_nm"),
             "f": sorted(f), "g": sorted(g), "i": sorted(i)}
        for lab in i:
            if lab in DISC_AMOUNT:
                a = disc_amount(corp, DISC_AMOUNT[lab], it.get("rcept_no"), bgn, end)
                if a and (a["amt"] or a["ratio"] is not None):
                    d["amt"], d["ratio"] = a["amt"], a["ratio"]
        out.append(d)
    return out


def fmt_pct(x, d=1):
    return f"{x * 100:.{d}f}%"


def explain(r):
    R, C = [], []
    if r.get("yld_ratio") and r["yld_ratio"] >= 1.1:
        R.append((r["yld_ratio"], f"기대 배당률 {r['exp_yield']:.1f}%로 최근 5년 평균 {r['yld_avg']:.1f}%보다 높음 — 배당 기준 역사적 저평가 구간"))
    if r["consec"] >= 5:
        inc = "삭감 없이 " if r["cuts"] == 0 else ""
        R.append((0.9 + r["consec"] / 20, f"{r['consec']}년 {'이상 ' if r['consec'] >= 6 else ''}연속 배당, {inc}주당배당금 3년 연평균 {fmt_pct(r['dps_cagr'] or 0)} 증가" if r.get("dps_cagr") else f"{r['consec']}년 연속 배당"))
    if r.get("per_rel") and r["per_rel"] < 0.85:
        R.append((1.5 - r["per_rel"], f"PER {r['per']:.1f}배로 업종({r['sector_name']}) 중앙값 {r['sec_per']:.1f}배 대비 {fmt_pct(1 - r['per_rel'], 0)} 할인"))
    if r.get("roe3") and r["roe3"] >= 12:
        R.append((r["roe3"] / 12, f"3년 평균 ROE {r['roe3']:.1f}%, 영업이익률 {r['opm']:.1f}%" if r.get("opm") else f"3년 평균 ROE {r['roe3']:.1f}%"))
    if r.get("eps_cagr") and r["eps_cagr"] >= 0.07:
        R.append((1 + r["eps_cagr"] * 3, f"순이익(EPS) 3년 연평균 {fmt_pct(r['eps_cagr'])} 성장" + (f", 매출 {fmt_pct(r['rev_cagr'])}" if r.get("rev_cagr") else "")))
    if r.get("peg") and r["peg"] < 1:
        R.append((1.4 - r["peg"] / 2, f"PEG {r['peg']:.2f} — 이익 성장 대비 주가 부담 낮음"))
    if r.get("fcf_cov") and r["fcf_cov"] >= 1.5:
        R.append((1 + min(r["fcf_cov"], 4) / 8, f"최근 3년 잉여현금흐름이 배당금의 {r['fcf_cov']:.1f}배 — 배당 여력 충분"))
    eo = ((r.get("est") or {}).get("op") or {})
    if eo.get("vs_last") is not None and eo["vs_last"] >= 5:
        val = eo.get("base")
        R.append((1.1 + eo["vs_last"] / 100, f"올해 영업이익 추정 {val:,.0f}억 — 전년 대비 {eo['vs_last']:+.0f}% ({r['est']['label']} 누적 기준)"))
    elif r.get("q_op_yoy") and r["q_op_yoy"] > 0.05:
        R.append((1 + r["q_op_yoy"], f"{r['q_label']} 누적 영업이익 전년 대비 {fmt_pct(r['q_op_yoy'], 0)} 증가"))
    R = [t for _, t in sorted(R, key=lambda x: -x[0])][:4]

    if r.get("exp_why"):
        C.append(f"표시 배당률 {r['cur_yield']:.1f}%는 작년 배당 기준 — 기대 배당률 {r['exp_yield']:.1f}%로 평가 ({r['exp_why']})")
    sr = r.get("share_ratio")
    if r.get("split_adj"):
        C.append(f"발행주식수가 {r['fiscal_label']} 말 대비 {sr:.2f}배 — 액면분할·병합 등으로 보고 과거 주당배당금·EPS를 현재 주식수 기준으로 환산")
    elif sr and abs(sr - 1) >= 0.05:
        C.append(f"발행주식수가 {r['fiscal_label']} 말 대비 {(sr - 1) * 100:+.0f}% — 증자·감자·소각 여부 확인 (PER은 시가총액 기준으로 계산)")
    dps = r["dps"]
    prev = [v for v in dps[2:5] if v]
    if dps[-1] and prev and dps[-1] > 1.6 * statistics.median(prev) and not r.get("exp_why"):
        C.append("최근 주당배당금이 직전 3년 대비 급증 — 특별배당 여부 확인")
    if r.get("yld_ratio") and r["yld_ratio"] > 1.8:
        C.append("배당률이 평소의 1.8배 이상 — 주가 급락 원인(실적·소송 등) 확인")
    if r["sector"] in CYCLICAL:
        C.append(f"경기민감 업종({r['sector_name']}) — 이익 정점에서 PER이 낮게 보일 수 있음")
    if r["sector"] in FINANCIAL:
        C.append("금융업 — 부채비율·현금흐름 지표는 점수에서 제외")
    if any(x in r["name"] for x in ("홀딩스", "지주")):
        C.append("지주회사 — 할인 해소 계기(자사주 소각·밸류업) 확인")
    ni, op = r["ni"][-1], r["op"][-1]
    if r["sector"] not in FINANCIAL and ni and op and op > 0 and ni > op * 1.3:
        C.append(f"순이익이 영업이익의 {ni / op:.1f}배 — 일회성 이익으로 보고 영업이익 기준 정상화 PER {r['per_norm']:.1f}배로 평가 (순이익 기준 {r['per_raw']:.1f}배)" if r.get("per_norm") and r.get("per_raw") else "순이익이 영업이익보다 크게 많음 — 일회성 이익 포함 여부 확인")
    if eo.get("vs_last") is not None and eo["vs_last"] <= -15:
        C.append(f"올해 영업이익 추정치가 전년보다 {abs(eo['vs_last']):.0f}% 적음 ({r['est']['label']} 누적 기준) — 실적 둔화")
    elif r.get("q_op_yoy") is not None and r["q_op_yoy"] < -0.15:
        C.append(f"{r['q_label']} 누적 영업이익 전년 대비 {fmt_pct(r['q_op_yoy'], 0)} — 실적 둔화")
    pf = (r.get("est") or {}).get("payout_fwd")
    if pf and pf > 90:
        C.append(f"올해 추정 이익 기준 배당성향 {pf:.0f}% — 현 배당 유지 부담")
    if r.get("ocf_ni") is not None and r["ocf_ni"] < 0.8:
        C.append(f"영업현금흐름이 순이익의 {r['ocf_ni']:.1f}배 — 이익의 현금 전환 약함")
    if r["cuts"]:
        C.append(f"최근 5년 중 배당 삭감 {r['cuts']}회")
    return R, C


def export(r, brief=False):
    base = {
        "code": r["code"], "name": r["name"], "market": r["market"], "sector": r["sector_name"],
        "price": r["price"], "mcap": r["mcap"], "score": r["score"], "cats": r["cats"],
        "yield": r1(r["exp_yield"], 2), "yield_shown": r1(r["cur_yield"], 2), "dps_exp": r1(r.get("dps_exp"), 0), "exp_why": r.get("exp_why"),
        "per": r1(r.get("per")), "dps": r1(r["dps"][-1], 1),
        "auditor": r.get("auditor"),
    }
    if brief:
        return base
    eok = lambda xs: [r1(v / 1e8, 0) if v is not None else None for v in xs]
    base.update({
        "pbr": r1(r.get("pbr"), 2), "roe3": r1(r.get("roe3")), "opm": r1(r.get("opm")), "debt": r1(r.get("debt"), 0),
        "rev_cagr": r1((r.get("rev_cagr") or 0) * 100) if r.get("rev_cagr") is not None else None,
        "eps_cagr": r1((r.get("eps_cagr") or 0) * 100) if r.get("eps_cagr") is not None else None,
        "dps_cagr": r1((r.get("dps_cagr") or 0) * 100) if r.get("dps_cagr") is not None else None,
        "q_op_yoy": r1(r["q_op_yoy"] * 100) if r.get("q_op_yoy") is not None else None,
        "payout": r1(r["payout"][-1]), "consec": r["consec"], "cuts": r["cuts"],
        "yld_avg": r1(r.get("yld_avg"), 2), "yld_min": r1(r.get("yld_min"), 2), "yld_max": r1(r.get("yld_max"), 2),
        "yld_ratio": r1(r.get("yld_ratio"), 2), "per_rel": r1(r.get("per_rel"), 2), "sec_per": r1(r.get("sec_per")),
        "peg": r1(r.get("peg"), 2), "ocf_ni": r1(r.get("ocf_ni"), 2), "fcf_cov": r1(r.get("fcf_cov"), 1),
        "series": {"dps": [r1(x, 1) for x in r["dps"]], "eps": [r1(x, 0) for x in r["eps"]], "yld": r["yld"], "payout": r["payout"],
                   "rev": eok(r["rev"]), "op": eok(r["op"]), "ni": eok(r["ni"]), "fcf": eok(r["fcf"] or [])},
        "reasons": r["reasons"], "cautions": r["cautions"], "fs": r.get("fs"),
        "est": r.get("est"), "per_fwd": r1(r.get("per_fwd")), "disc": r.get("disc", []),
        "auditor_src": r.get("auditor_src"), "bs_src": r.get("bs_src"),
        "share_ratio": r1(r.get("share_ratio"), 3), "split_adj": bool(r.get("split_adj")),
        "per_raw": r1(r.get("per_raw")), "per_norm": r1(r.get("per_norm")), "one_off": bool(r.get("one_off")),
        "roe3_raw": r1(r.get("roe3_raw")),
        "payout_flag": bool(r["payout"][-1] and r["payout"][-1] >= CFG["payout_flag"]),
    })
    return base


if __name__ == "__main__":
    main()
