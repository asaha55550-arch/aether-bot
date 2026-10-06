import ccxt
import pandas as pd
import requests
import os
from flask import Flask, jsonify
from datetime import datetime

app = Flask(__name__)

# টেলিগ্রাম ক্রেডেনশিয়ালস
TELEGRAM_BOT_TOKEN = "8743022961:AAHyGe2Kf8RWUhIpKEJ4QbDmvJdfphZPFDM"
CHAT_ID = "7786764916"

@app.route('/')
def run_scanner():
    exchange = ccxt.hyperliquid({
        'enableRateLimit': True,
        'timeout': 15000
    })
    
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
        hl_pairs = []
        for symbol, ticker in tickers.items():
            market = exchange.market(symbol)
            if market['swap'] and ticker.get('quoteVolume') is not None:
                if ticker['quoteVolume'] > 1000000: 
                    hl_pairs.append({'symbol': symbol, 'volume': ticker['quoteVolume']})
        hl_pairs = sorted(hl_pairs, key=lambda x: x['volume'], reverse=True)
        top_symbols = [x['symbol'] for x in hl_pairs[:50]]
    except Exception as e:
        return jsonify({"error": f"Failed to fetch markets: {e}"})

    alerts = []
    current_time_ms = exchange.milliseconds()

    for symbol in top_symbols:
        try:
            bars_5m = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
            if not bars_5m or len(bars_5m) < 40: continue
            
            df_5m = pd.DataFrame(bars_5m, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            
            c1 = df_5m.iloc[-5]  
            c2 = df_5m.iloc[-4]  
            c3 = df_5m.iloc[-3]  
            c4 = df_5m.iloc[-2]  

            def get_metrics(c):
                r = c['high'] - c['low']
                b = abs(c['close'] - c['open'])
                return r, b

            c1_range, c1_body = get_metrics(c1)
            c2_range, c2_body = get_metrics(c2)
            c3_range, c3_body = get_metrics(c3)
            
            if c1_range == 0 or c2_range == 0 or c3_range == 0: continue

            # Pattern Math
            is_bull_c1 = (c1['close'] > c1['open']) and (c1_body >= c1_range * 0.45)
            is_bear_c1 = (c1['close'] < c1['open']) and (c1_body >= c1_range * 0.45)
            
            is_doji = (c2_body <= c2_range * 0.35) and (c2['high'] <= c1['high']) and (c2['low'] >= c1['low'])
            
            is_bull_c3 = (c3['close'] > c3['open']) and (c3_body >= c3_range * 0.45)
            is_bear_c3 = (c3['close'] < c3['open']) and (c3_body >= c3_range * 0.45)
            strong_c3 = c3_range >= (c1_range * 0.60)

            bull_breakout = is_bull_c1 and is_doji and is_bull_c3 and strong_c3 and (c3['close'] > c2['high'])
            bear_breakout = is_bear_c1 and is_doji and is_bear_c3 and strong_c3 and (c3['close'] < c2['low'])

            if not (bull_breakout or bear_breakout): continue 

            bull_retest = bull_breakout and (c4['low'] <= c2['high'] * 1.002) and (c4['close'] > c2['high'])
            bear_retest = bear_breakout and (c4['high'] >= c2['low'] * 0.998) and (c4['close'] < c2['low'])

            if not (bull_retest or bear_retest): continue

            c4_close_time = c4['time'] + (5 * 60 * 1000)
            time_since_c4_close = (current_time_ms - c4_close_time) / (1000 * 60)
            if time_since_c4_close > 5.5: continue

            df_5m['prev_close'] = df_5m['close'].shift(1)
            df_5m['tr1'] = df_5m['high'] - df_5m['low']
            df_5m['tr2'] = abs(df_5m['high'] - df_5m['prev_close'])
            df_5m['tr3'] = abs(df_5m['low'] - df_5m['prev_close'])
            df_5m['tr'] = df_5m[['tr1', 'tr2', 'tr3']].max(axis=1)
            atr_5m = df_5m['tr'].rolling(14).mean().iloc[-2] 

            vol_avg = df_5m['volume'].iloc[-23:-3].mean() 
            volume_ok = c3['volume'] >= (vol_avg * 1.2)

            def check_mtf(tf):
                try:
                    bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=60)
                    df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
                    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
                    curr = df.iloc[-2]
                    prev = df.iloc[-3]
                    bull = curr['close'] > curr['ema_50'] and curr['ema_50'] > prev['ema_50']
                    bear = curr['close'] < curr['ema_50'] and curr['ema_50'] < prev['ema_50']
                    return bull, bear
                except:
                    return False, False

            is_15m_bull, is_15m_bear = check_mtf('15m')
            is_1h_bull, is_1h_bear = check_mtf('1h')

            score = 50 
            if volume_ok: score += 10
            if atr_5m > (c2_range * 0.5): score += 10 

            is_long = bull_retest
            if is_long:
                if is_15m_bull: score += 15
                if is_1h_bull: score += 15
            else:
                if is_15m_bear: score += 15
                if is_1h_bear: score += 15

            if score < 30: continue

            entry_price = c4['close']
            clean_name = symbol.split('/')[0]
            trade_link = f"https://app.hyperliquid.xyz/trade/{clean_name}"

            if is_long:
                sl = c1['low'] - (atr_5m * 0.5)
                risk = entry_price - sl
                tp1 = entry_price + (risk * 1.5)
                tp2 = entry_price + (risk * 2.5)
                risk_pct = round((risk / entry_price) * 100, 2)
                
                alerts.append(
                    f"🥪 *PRO SANDWICH — LONG*\n\n"
                    f"🪙 *{clean_name}* [Trade]({trade_link})\n"
                    f"⭐ *Score:* `{score}/100` (Grade: {'A+' if score>=90 else 'A' if score>=80 else 'B' if score>=50 else 'C'})\n\n"
                    f"📊 *Confluence*\n"
                    f"• 1H: {'🟢 Bull' if is_1h_bull else '⚪ Neutral'}\n"
                    f"• 15M: {'🟢 Bull' if is_15m_bull else '⚪ Neutral'}\n"
                    f"• Vol (C3): {round(c3['volume']/vol_avg, 2)}x Avg\n\n"
                    f"💰 *Entry (C4 Close):* `{round(entry_price, 4)}`\n"
                    f"🛑 *SL:* `{round(sl, 4)}`\n"
                    f"🎯 *TP1:* `{round(tp1, 4)}` | *TP2:* `{round(tp2, 4)}`\n\n"
                    f"⚖️ *Risk:* `{risk_pct}%` | 📐 *R:R:* `1:2.5`"
                )
            else:
                sl = c1['high'] + (atr_5m * 0.5)
                risk = sl - entry_price
                tp1 = entry_price - (risk * 1.5)
                tp2 = entry_price - (risk * 2.5)
                risk_pct = round((risk / entry_price) * 100, 2)
                
                alerts.append(
                    f"🥪 *PRO SANDWICH — SHORT*\n\n"
                    f"🪙 *{clean_name}* [Trade]({trade_link})\n"
                    f"⭐ *Score:* `{score}/100` (Grade: {'A+' if score>=90 else 'A' if score>=80 else 'B' if score>=50 else 'C'})\n\n"
                    f"📊 *Confluence*\n"
                    f"• 1H: {'🔴 Bear' if is_1h_bear else '⚪ Neutral'}\n"
                    f"• 15M: {'🔴 Bear' if is_15m_bear else '⚪ Neutral'}\n"
                    f"• Vol (C3): {round(c3['volume']/vol_avg, 2)}x Avg\n\n"
                    f"💰 *Entry (C4 Close):* `{round(entry_price, 4)}`\n"
                    f"🛑 *SL:* `{round(sl, 4)}`\n"
                    f"🎯 *TP1:* `{round(tp1, 4)}` | *TP2:* `{round(tp2, 4)}`\n\n"
                    f"⚖️ *Risk:* `{risk_pct}%` | 📐 *R:R:* `1:2.5`"
                )

        except Exception as e:
            print(f"Error processing {symbol}: {str(e)}")
            continue
            
    if len(alerts) > 0:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        msg = f"🔥 *AETHER QUANT SYSTEM* 🔥\n⏱ {now_str}\n\n" + "\n\n---\n\n".join(alerts)
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
            "chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True
        }, timeout=10) 
        return jsonify({"status": "Success", "signals": len(alerts)})
    else:
        return jsonify({"status": "Scanned 50 coins. No setups found."})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
