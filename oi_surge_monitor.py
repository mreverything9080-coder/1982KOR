#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[KJ] OI 급증 감지기 (Bybit Open Interest Surge Monitor) — Tier 2 구현체
─────────────────────────────────────────────────────────────────
목적: "직교(orthogonal) 축" 데이터인 OI(Open Interest, 미결제약정)의 급증만을
     따로 감시해 "사전 신호(가격이 아직 안 움직인 상태에서 포지션이 먼저 쌓이는 구간)"를
     "진행형 신호(가격이 이미 움직이는 중 OI도 같이 증가)"와 구분해 태깅한다.

데이터 소스: Bybit V5 공개 API (GET /v5/market/open-interest, GET /v5/market/kline)
           → 두 엔드포인트 모두 인증(API 키) 불필요, GitHub Actions에서 바로 호출 가능.

실행 방식: GitHub Actions cron (예: 15분 간격) → 결과를 텔레그램으로 발송
         + data/oi_surge_log.json 에 스냅샷 누적 (Tier 3 승률 검증용 원본 로그 겸용)

⚠ 주의: 이 컨테이너 환경은 api.bybit.com에 대한 아웃바운드 네트워크가 막혀 있어
        여기서 실행/테스트가 불가능합니다. GitHub Actions 환경(오픈 인터넷)에서
        실행해야 합니다. 아래 로직은 Bybit V5 공식 문서 스펙 기준으로 작성했습니다.
"""

import os
import json
import requests
from datetime import datetime, timezone

# ── 설정 ──────────────────────────────────────────────────────────
BYBIT_BASE = "https://api.bybit.com"
CATEGORY = "linear"            # USDT 무기한 선물(Perpetual)
OI_INTERVAL = "15min"          # Bybit 최소 집계 단위: 5min/15min/30min/1h/4h/1d
OI_LOOKBACK = 150              # 15min*150 = 37.5시간 커버 (24h 변화율 계산에 여유 확보)

# 감시 심볼: 우선 소수로 시작. 실제 운용 시 Tier 1 Pine Screener에서 넘어온
# "BBW 압축 후보" 리스트로 교체하는 것을 권장 (아래 load_watchlist() 참고)
DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
CANDIDATE_FILE = "data/tier1_candidates.json"  # Tier1 스크리너 결과물이 있으면 우선 사용

# 급증 임계값(%) — 기존 '유입 성격 스코어보드'의 24h 기준(+5%)보다 짧은 창을 추가
SURGE_THRESHOLDS = {"1h": 3.0, "4h": 6.0, "24h": 10.0}

# 가격 정체 판정 상한(%) — 이 이내면 "가격은 조용, OI만 급증" = 사전 신호 후보로 태깅
PRICE_FLAT_MAX_PCT = 1.5

LOG_PATH = "data/oi_surge_log.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ── Bybit API 호출 ────────────────────────────────────────────────
def fetch_open_interest(symbol: str):
    """GET /v5/market/open-interest — 인증 불필요. 반환: [(timestamp_ms, oi_value), ...] 오름차순"""
    url = f"{BYBIT_BASE}/v5/market/open-interest"
    params = {"category": CATEGORY, "symbol": symbol, "intervalTime": OI_INTERVAL, "limit": OI_LOOKBACK}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"{symbol} OI 조회 실패: {data.get('retMsg')}")
    rows = data["result"]["list"]
    rows = sorted(rows, key=lambda x: int(x["timestamp"]))  # 반환 순서에 의존하지 않도록 방어적 정렬
    return [(int(x["timestamp"]), float(x["openInterest"])) for x in rows]


def fetch_price_change_pct(symbol: str, minutes: int) -> float:
    """GET /v5/market/kline — 같은 윈도우(minutes) 동안의 가격 변화율(%). 인증 불필요."""
    url = f"{BYBIT_BASE}/v5/market/kline"
    params = {"category": CATEGORY, "symbol": symbol, "interval": "15", "limit": max(2, minutes // 15)}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    rows = r.json()["result"]["list"]  # [startTime, open, high, low, close, volume, turnover], 최신순
    closes = [float(x[4]) for x in rows]
    closes.reverse()
    if len(closes) < 2 or closes[0] == 0:
        return 0.0
    return (closes[-1] - closes[0]) / closes[0] * 100


def pct_change_at(series, minutes_ago: int, now_ts: int) -> float:
    """series: [(ts_ms, oi), ...] 오름차순. now_ts 기준 minutes_ago분 전과 최신값 비교."""
    target_ts = now_ts - minutes_ago * 60_000
    past = min(series, key=lambda p: abs(p[0] - target_ts))
    latest_val = series[-1][1]
    if past[1] == 0:
        return 0.0
    return (latest_val - past[1]) / past[1] * 100


# ── 후보 리스트 로딩 (Tier 1 연동 지점) ───────────────────────────
def load_watchlist():
    """
    Tier 1 Pine Screener 결과(BBW 압축 후보 등)를 data/tier1_candidates.json 으로 내보내면
    자동으로 그 리스트만 조회 — 전체 심볼을 매 사이클 호출하는 중복 수집을 방지.
    파일이 없으면 DEFAULT_WATCHLIST로 폴백.
    형식 예: {"symbols": ["BTCUSDT", "ARBUSDT", ...]}
    """
    if os.path.exists(CANDIDATE_FILE):
        with open(CANDIDATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            symbols = data.get("symbols", [])
            if symbols:
                return symbols
    return DEFAULT_WATCHLIST


# ── 알림 / 로그 ───────────────────────────────────────────────────
def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[경고] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — 알림 생략, 콘솔 출력만 진행")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    if resp.status_code != 200:
        print(f"[경고] 텔레그램 발송 실패: {resp.status_code} {resp.text}")


def load_log():
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_log(log):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


# ── 메인 루프 ─────────────────────────────────────────────────────
def main():
    now_kst = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M KST")
    log = load_log()
    watchlist = load_watchlist()
    print(f"[{now_kst}] 감시 대상 {len(watchlist)}종목: {watchlist}")

    for symbol in watchlist:
        try:
            oi_series = fetch_open_interest(symbol)
        except Exception as e:
            print(f"[에러] {symbol} OI 조회 실패: {e}")
            continue

        if len(oi_series) < 5:
            print(f"[스킵] {symbol}: OI 데이터 부족({len(oi_series)}개)")
            continue

        now_ts = oi_series[-1][0]
        chg_1h = pct_change_at(oi_series, 60, now_ts)
        chg_4h = pct_change_at(oi_series, 240, now_ts)
        chg_24h = pct_change_at(oi_series, 1440, now_ts)

        try:
            price_chg_1h = fetch_price_change_pct(symbol, 60)
        except Exception as e:
            print(f"[에러] {symbol} 가격 조회 실패: {e}")
            price_chg_1h = None

        entry = {
            "time": now_kst, "symbol": symbol,
            "oi_1h_pct": round(chg_1h, 2), "oi_4h_pct": round(chg_4h, 2), "oi_24h_pct": round(chg_24h, 2),
            "price_1h_pct": round(price_chg_1h, 2) if price_chg_1h is not None else None,
        }
        log.append(entry)

        surged = chg_1h >= SURGE_THRESHOLDS["1h"] or chg_4h >= SURGE_THRESHOLDS["4h"] or chg_24h >= SURGE_THRESHOLDS["24h"]
        if not surged:
            continue

        # 핵심 분기: 가격이 조용한데 OI만 급증 = 사전 신호 후보 / 가격도 이미 급변 = 진행(확인)형
        if price_chg_1h is None:
            tag = "⚪ 판정 보류 (가격 데이터 조회 실패)"
        elif abs(price_chg_1h) <= PRICE_FLAT_MAX_PCT:
            tag = "🟡 사전 신호 후보 — 가격 정체 + OI만 급증 (신규 포지션 선진입 추정)"
        else:
            tag = "🔴 진행형 신호 — 가격도 이미 움직이는 중 (사전 신호 아님, 추격 주의)"

        msg = (
            f"⚡ *OI 급증 감지* — `{symbol}`\n"
            f"{now_kst}\n"
            f"OI 변화율: 1h {chg_1h:+.1f}% · 4h {chg_4h:+.1f}% · 24h {chg_24h:+.1f}%\n"
            f"가격 변화(1h): {price_chg_1h:+.1f}%\n" if price_chg_1h is not None else f"가격 변화(1h): 조회 실패\n"
        ) + f"판정: {tag}"

        send_telegram(msg)
        print(msg)

    save_log(log)
    print(f"[{now_kst}] 완료 — 누적 로그 {len(log)}건")


if __name__ == "__main__":
    main()
