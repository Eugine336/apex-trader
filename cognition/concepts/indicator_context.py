"""Contextual technical-indicator readings from completed OHLC history.

Indicator values are interpreted as conditional market observations, not votes.
All lookbacks and formula conventions are explicit; insufficient history stays absent.
"""
from __future__ import annotations

import math
from statistics import mean, pstdev

from cognition.concepts.base import (
    FLAT, LONG, SHORT, TIMEFRAME_SECONDS, ConceptReading, Implication,
    finite, percentile_rank, reference_price,
)
from cognition.contracts import EvidenceDomain


def _clean(rows):
    out = []
    for row in rows or []:
        o,h,l,c=(finite(row.get(k)) for k in ("open","high","low","close"))
        if None in (o,h,l,c) or h<l or h<max(o,c) or l>min(o,c):
            continue
        out.append({**row,"open":o,"high":h,"low":l,"close":c})
    return out


def _ema(values, period):
    if len(values)<period: return []
    alpha=2.0/(period+1.0)
    current=mean(values[:period])
    out=[None]*(period-1)+[current]
    for value in values[period:]:
        current=alpha*value+(1-alpha)*current
        out.append(current)
    return out


def _rsi(values, period=14):
    if len(values)<period+1: return None
    changes=[b-a for a,b in zip(values,values[1:])]
    gains=[max(x,0.0) for x in changes]
    losses=[max(-x,0.0) for x in changes]
    avg_gain=mean(gains[:period]); avg_loss=mean(losses[:period])
    for gain,loss in zip(gains[period:],losses[period:]):
        avg_gain=(avg_gain*(period-1)+gain)/period
        avg_loss=(avg_loss*(period-1)+loss)/period
    if avg_gain==0 and avg_loss==0: return 50.0
    if avg_loss==0: return 100.0
    return 100.0-100.0/(1.0+avg_gain/avg_loss)


def _true_ranges(bars):
    out=[]
    for i,b in enumerate(bars):
        prev=bars[i-1]["close"] if i else b["close"]
        out.append(max(b["high"]-b["low"],abs(b["high"]-prev),abs(b["low"]-prev)))
    return out


def _wilder(values, period):
    if len(values)<period: return []
    current=sum(values[:period])
    out=[None]*(period-1)+[current]
    for value in values[period:]:
        current=current-current/period+value
        out.append(current)
    return out


def _read(concept,tf,domain,bars,ref,ref_tf,observation,implications,facts,unknowns=()):
    last=bars[-1]
    epoch=finite(last.get("timestamp_epoch")) if last.get("source_timestamp_verified") is True else None
    return ConceptReading(
        concept=concept,timeframe=tf,domain=domain,applicable=True,
        applicability_reason="The required completed-bar history and formula inputs are present",
        observation=observation,implications=tuple(implications),facts=facts,
        source_bar_epoch=epoch,reference_close=ref,reference_timeframe=ref_tf,
        unknowns=tuple(unknowns),
    )


def _imp(direction,statement,active,requires=(),conflicts=()):
    return Implication(
        direction=direction,statement=statement,strength=None,activated=bool(active),
        activation_reason=statement if active else "The declared indicator condition is not satisfied",
        invalidation_reason="Reassess on the next completed bar and on structural change",
        requires=tuple(requires),conflicts_with=tuple(conflicts),
        management={"role":"context_only","reassess_on":"next_completed_bar"},
    )


class IndicatorContextConcepts:
    key="indicator_context"
    family="technical_indicator_context"
    covers_methods=(
        "moving_averages","rsi","macd","stochastic","cci_williams","roc_momentum",
        "adx_directional","aroon_trix","atr_true_range","bollinger",
        "keltner_donchian","realized_volatility","zscore_mean",
    )
    covers_lenses=()

    def read(self,symbol: str,bars_by_tf: dict[str,list[dict]]) -> list[ConceptReading]:
        ref,ref_tf=reference_price(bars_by_tf); out=[]
        for tf,raw in bars_by_tf.items():
            if tf not in TIMEFRAME_SECONDS or TIMEFRAME_SECONDS[tf]<300: continue
            bars=_clean(raw)
            if len(bars)<60: continue
            out.extend(self._trend(tf,bars,ref,ref_tf))
            out.extend(self._momentum(tf,bars,ref,ref_tf))
            out.extend(self._volatility(tf,bars,ref,ref_tf))
            out.extend(self._statistics(tf,bars,ref,ref_tf))
        return out

    def _trend(self,tf,bars,ref,ref_tf):
        closes=[b["close"] for b in bars]; highs=[b["high"] for b in bars]; lows=[b["low"] for b in bars]
        ema20=_ema(closes,20); ema50=_ema(closes,50)
        if not ema20 or not ema50: return []
        e20,e50=ema20[-1],ema50[-1]
        def wma(values):
            n=len(values)
            return sum(v*w for w,v in enumerate(values,1))/(n*(n+1)/2.0) if n else None
        wma20=wma(closes[-20:]); sma20=mean(closes[-20:])
        hma_diffs=[]
        for end in range(len(closes)-3,len(closes)):
            series=closes[:end+1]
            hma_diffs.append(2*wma(series[-10:])-wma(series[-20:]))
        hma20=wma(hma_diffs[-4:])
        volumes=[finite(b.get("volume")) for b in bars[-20:]]
        vwma20=None
        if all(v is not None and v>=0 for v in volumes) and sum(volumes)>0:
            vwma20=sum(p*v for p,v in zip(closes[-20:],volumes))/sum(volumes)
        side=LONG if e20>e50 and closes[-1]>e20 else SHORT if e20<e50 and closes[-1]<e20 else FLAT
        aligned=side in (LONG,SHORT)
        out=[_read(
            "moving_averages",tf,EvidenceDomain.MOMENTUM,bars,ref,ref_tf,
            f"SMA20={sma20:.10g}; EMA20={e20:.10g}; EMA50={e50:.10g}; WMA20={wma20:.10g}; HMA20={hma20:.10g}; VWMA20={vwma20}; alignment={side}.",
            [_imp(side,f"{side} moving-average alignment" if aligned else "Moving averages are mixed or unaligned",
                  aligned,("confirmed market structure","trend persistence and volatility context"))],
            {"sma20":sma20,"ema20":e20,"ema50":e50,"wma20":wma20,"hma20":hma20,
             "vwma20":vwma20,"volume_available":vwma20 is not None,
             "volume_source":bars[-1].get("volume_source","unknown") if vwma20 is not None else None,
             "close":closes[-1],"ema20_minus_ema50":e20-e50,"alignment":side,
             "definition":"sma20_ema20_ema50_wma20_hma20_optional_vwma20_v1"},
            ("Moving averages lag and can whipsaw in ranges; VWMA is omitted without a declared volume series.",)
        )]
        # Wilder directional movement and ADX; strength is separated from direction.
        tr=_true_ranges(bars); plus_dm=[0.0]; minus_dm=[0.0]
        for prev,current in zip(bars,bars[1:]):
            up=current["high"]-prev["high"]; down=prev["low"]-current["low"]
            plus_dm.append(up if up>down and up>0 else 0.0)
            minus_dm.append(down if down>up and down>0 else 0.0)
        tr_s,plus_s,minus_s=_wilder(tr,14),_wilder(plus_dm,14),_wilder(minus_dm,14)
        dx=[]
        for a,b,d in zip(tr_s,plus_s,minus_s):
            if a is None or b is None or d is None or a<=0: continue
            pdi,mdi=100*b/a,100*d/a
            dx.append(100*abs(pdi-mdi)/(pdi+mdi) if pdi+mdi>0 else 0.0)
        if dx:
            adx_series=_wilder(dx,14); adx=adx_series[-1]/14.0 if adx_series and adx_series[-1] is not None else mean(dx[-14:]); pdi=100*plus_s[-1]/tr_s[-1] if tr_s[-1]>0 else 0.0
            mdi=100*minus_s[-1]/tr_s[-1] if tr_s[-1]>0 else 0.0
            di_side=LONG if pdi>mdi else SHORT if mdi>pdi else FLAT
            strong=adx>=25.0
            out.append(_read(
                "adx_directional",tf,EvidenceDomain.MOMENTUM,bars,ref,ref_tf,
                f"ADX14={adx:.2f}; +DI={pdi:.2f}; -DI={mdi:.2f}; strength={'stronger' if strong else 'weak_or_mixed'}; direction={di_side}.",
                [_imp(di_side,f"{di_side} directional movement with ADX14={adx:.2f}",strong and di_side!=FLAT,
                      ("price structure and regime context",))],
                {"adx14":adx,"plus_di14":pdi,"minus_di14":mdi,"direction":di_side,
                 "strength_state":"stronger" if strong else "weak_or_mixed",
                 "conventional_strength_reference":25.0,"definition":"wilder_adx14_v1"},
                ("ADX measures trend strength, not direction; 25 is a conventional reference, not a universal edge threshold.",)
            ))
        # Aroon locates recent extrema; TRIX describes triple-smoothed momentum.
        period=25; wh=highs[-period:]; wl=lows[-period:]
        aroon_up=100.0*(wh.index(max(wh))+1)/period
        aroon_down=100.0*(wl.index(min(wl))+1)/period
        ema1=[x for x in _ema(closes,15) if x is not None]
        ema2=[x for x in _ema(ema1,15) if x is not None]
        ema3=_ema(ema2,15)
        trix=((ema3[-1]/ema3[-2])-1.0)*100.0 if len(ema3)>=2 and ema3[-2] else 0.0
        aroon_side=LONG if aroon_up>aroon_down and trix>0 else SHORT if aroon_down>aroon_up and trix<0 else FLAT
        out.append(_read(
            "aroon_trix",tf,EvidenceDomain.MOMENTUM,bars,ref,ref_tf,
            f"Aroon up={aroon_up:.2f}; Aroon down={aroon_down:.2f}; TRIX15={trix:.5f}%; combined state={aroon_side}.",
            [_imp(aroon_side,f"{aroon_side} Aroon/TRIX alignment" if aroon_side!=FLAT else "Aroon and TRIX are mixed",
                  aroon_side!=FLAT,("confirmed price structure","volatility and transaction-cost context"))],
            {"aroon_up25":aroon_up,"aroon_down25":aroon_down,"trix15_percent":trix,
             "directional_state":aroon_side,"definition":"aroon25_and_triple_ema_trix15_v1"},
            ("Aroon and TRIX are lagging price transforms and are not independent confirmations.",)
        ))
        return out

    def _momentum(self,tf,bars,ref,ref_tf):
        closes=[b["close"] for b in bars]; highs=[b["high"] for b in bars]; lows=[b["low"] for b in bars]
        rsi=_rsi(closes,14)
        e12,e26=_ema(closes,12),_ema(closes,26)
        macd=[a-b for a,b in zip(e12,e26) if a is not None and b is not None]
        signal=_ema(macd,9)
        hist=macd[-1]-signal[-1] if signal and signal[-1] is not None else None
        hh=max(highs[-14:]); ll=min(lows[-14:])
        stoch=(100*(closes[-1]-ll)/(hh-ll)) if hh>ll else 50.0
        k_values=[]
        for i in range(len(bars)-14,len(bars)):
            hi=max(highs[i-13:i+1]); lo=min(lows[i-13:i+1])
            k_values.append(100*(closes[i]-lo)/(hi-lo) if hi>lo else 50.0)
        stoch_d=mean(k_values[-3:])
        typical=[(b["high"]+b["low"]+b["close"])/3 for b in bars[-20:]]
        tp_mean=mean(typical); mean_dev=mean(abs(x-tp_mean) for x in typical)
        cci=(typical[-1]-tp_mean)/(0.015*mean_dev) if mean_dev>0 else 0.0
        highest=max(highs[-14:]); lowest=min(lows[-14:])
        williams=-100*(highest-closes[-1])/(highest-lowest) if highest>lowest else -50.0
        roc=(closes[-1]/closes[-13]-1.0)*100.0 if closes[-13] else None
        macd_side=LONG if hist is not None and hist>0 else SHORT if hist is not None and hist<0 else FLAT
        oscillator_state="overbought" if rsi>=70 else "oversold" if rsi<=30 else "neutral"
        aligned=macd_side in (LONG,SHORT) and roc is not None and ((roc>0)==(macd_side==LONG))
        return [_read(
            "rsi",tf,EvidenceDomain.MOMENTUM,bars,ref,ref_tf,
            f"RSI14={rsi:.2f} ({oscillator_state}); conventional 70/30 extremes are stretch descriptors, not reversal triggers.",
            [_imp(FLAT,"RSI describes recent gain/loss balance; extreme readings can persist in trends",True,
                  ("price structure and trend regime",))],
            {"rsi14":rsi,"state":oscillator_state,"overbought_70":rsi>=70,"oversold_30":rsi<=30,
             "definition":"wilder_rsi14_v1"},
            ("RSI thresholds are conventions, not calibrated probabilities.",)
        ),_read(
            "macd",tf,EvidenceDomain.MOMENTUM,bars,ref,ref_tf,
            f"MACD12-26={macd[-1]:.10g}; signal9={signal[-1]:.10g}; histogram={hist:.10g}; sign={macd_side}.",
            [_imp(macd_side,f"{macd_side} MACD momentum state",hist is not None and hist!=0,
                  ("price structure","momentum persistence","transaction costs"))],
            {"macd":macd[-1],"signal":signal[-1],"histogram":hist,"directional_state":macd_side,
             "definition":"ema12_26_signal9_sma_seed_v1"},
            ("MACD is a lagging transformation of price and is not independent confirmation.",)
        ),_read(
            "stochastic",tf,EvidenceDomain.MOMENTUM,bars,ref,ref_tf,
            f"Stochastic %K14={stoch:.2f}; %D3={stoch_d:.2f}; range location is descriptive.",
            [_imp(FLAT,"Stochastic locates close within its recent high-low range",True,
                  ("range/trend regime","confirmed price structure"))],
            {"stochastic_k14":stoch,"stochastic_d3":stoch_d,"definition":"stochastic_14_3_v1"},
            ("Oscillators can remain extreme during persistent trends.",)
        ),_read(
            "cci_williams",tf,EvidenceDomain.MOMENTUM,bars,ref,ref_tf,
            f"CCI20={cci:.2f}; Williams %R14={williams:.2f}; both describe price displacement within recent ranges.",
            [_imp(FLAT,"CCI and Williams %R are range-normalized displacement measures",True,
                  ("market regime and price structure",))],
            {"cci20":cci,"williams_r14":williams,"definition":"cci20_williams14_v1"},
            ("Thresholds are not universal and neither oscillator alone establishes reversal.",)
        ),_read(
            "roc_momentum",tf,EvidenceDomain.MOMENTUM,bars,ref,ref_tf,
            f"12-bar rate of change={roc:.4f}%; MACD histogram={hist:.10g}; directional momentum alignment={aligned}.",
            [_imp(macd_side if aligned else FLAT,
                  f"{'Aligned' if aligned else 'Mixed'} rate-of-change and MACD momentum",
                  aligned,("structure and volatility context",))],
            {"roc12_percent":roc,"macd_histogram":hist,"aligned":aligned,
             "definition":"close_12_bar_roc_and_macd_histogram_v1"},
            ("Momentum measures are correlated transformations of the same price series.",)
        )]

    def _volatility(self,tf,bars,ref,ref_tf):
        closes=[b["close"] for b in bars]; highs=[b["high"] for b in bars]; lows=[b["low"] for b in bars]
        trs=_true_ranges(bars); atrs=[]
        for end in range(14,len(trs)+1):
            atrs.append(mean(trs[end-14:end]))
        atr=atrs[-1]; atr_rank=percentile_rank(atr,atrs[:-1])
        window=closes[-20:]; centre=mean(window); sigma=pstdev(window)
        upper,lower=centre+2*sigma,centre-2*sigma
        width=(upper-lower)/centre if centre else None
        ema20=_ema(closes,20)[-1]
        k_upper,k_lower=ema20+2*atr,ema20-2*atr
        d_high,d_low=max(highs[-20:]),min(lows[-20:])
        log_returns=[math.log(b/a) for a,b in zip(closes[-21:],closes[-20:]) if a>0 and b>0]
        realized=pstdev(log_returns) if len(log_returns)>1 else None
        parkinson=math.sqrt(mean((math.log(h/l)**2 for h,l in zip(highs[-20:],lows[-20:]) if h>0 and l>0))/(4*math.log(2)))
        state="high_relative_volatility" if atr_rank is not None and atr_rank>=0.8 else "low_relative_volatility" if atr_rank is not None and atr_rank<=0.2 else "middle_or_unknown"
        return [_read(
            "atr_true_range",tf,EvidenceDomain.VOLATILITY,bars,ref,ref_tf,
            f"ATR14={atr:.10g}; relative percentile={atr_rank}; state={state}.",
            [_imp(FLAT,"ATR measures range magnitude, not direction",True,("instrument price scale and execution costs",))],
            {"atr14":atr,"atr_percentile":atr_rank,"state":state,"definition":"simple_rolling_atr14_and_history_percentile_v1"},
            ("ATR percentile requires sufficient prior ATR observations; it is not a volatility forecast.",)
        ),_read(
            "bollinger",tf,EvidenceDomain.VOLATILITY,bars,ref,ref_tf,
            f"Bollinger20 centre={centre:.10g}; upper={upper:.10g}; lower={lower:.10g}; width={width}.",
            [_imp(FLAT,"Bollinger envelope describes dispersion around a rolling mean",True,
                  ("trend regime and follow-through",))],
            {"middle":centre,"upper":upper,"lower":lower,"width":width,
             "percent_b":(closes[-1]-lower)/(upper-lower) if upper>lower else None,
             "definition":"sma20_population_std_two_sigma_v1"},
            ("Band touches are not standalone reversal or breakout evidence.",)
        ),_read(
            "keltner_donchian",tf,EvidenceDomain.VOLATILITY,bars,ref,ref_tf,
            f"Keltner20 envelope={k_lower:.10g}–{k_upper:.10g}; Donchian20={d_low:.10g}–{d_high:.10g}.",
            [_imp(FLAT,"Keltner and Donchian envelopes locate price relative to volatility and recent extremes",True,
                  ("breakout acceptance and market regime",))],
            {"keltner_lower":k_lower,"keltner_upper":k_upper,"donchian_low":d_low,
             "donchian_high":d_high,"definition":"ema20_plusminus_2_atr14_and_donchian20_v1"},
            ("Channel boundaries are rolling references and can be crossed repeatedly.",)
        ),_read(
            "realized_volatility",tf,EvidenceDomain.VOLATILITY,bars,ref,ref_tf,
            f"20-return realized log volatility={realized}; Parkinson OHLC volatility={parkinson:.10g}.",
            [_imp(FLAT,"Realized and range-based estimators describe historical movement dispersion",True,
                  ("sampling interval and feed quality",))],
            {"realized_log_return_std20":realized,"parkinson_ohlc20":parkinson,
             "definition":"20_bar_log_return_std_and_parkinson_ohlc_v1"},
            ("Estimators are sampling-frequency dependent and are not forward volatility forecasts.",)
        )]

    def _statistics(self,tf,bars,ref,ref_tf):
        closes=[b["close"] for b in bars]; window=closes[-20:]
        centre=mean(window); sigma=pstdev(window)
        z=(closes[-1]-centre)/sigma if sigma>0 else 0.0
        # A rolling distribution comparison is a diagnostic, not a formal stationarity test.
        first=closes[-40:-20]; second=closes[-20:]
        mean_shift=mean(second)-mean(first)
        vol_ratio=(pstdev(second)/pstdev(first)) if pstdev(first)>0 else None
        stretched=abs(z)>=2.0
        return [_read(
            "zscore_mean",tf,EvidenceDomain.MOMENTUM,bars,ref,ref_tf,
            f"20-bar price Z-score={z:.3f}; rolling mean shift={mean_shift:.10g}; "
            f"second/first half volatility ratio={vol_ratio}.",
            [_imp(FLAT,"Statistical displacement is a stretch observation, not proof of mean reversion",True,
                  ("stable distribution","weak directional persistence","transaction-cost-adjusted reversion evidence"),
                  ("distribution shift or volatility regime change",) if vol_ratio is not None and vol_ratio>2 else ())],
            {"zscore20":z,"rolling_mean20":centre,"rolling_std20":sigma,
             "half_window_mean_shift":mean_shift,"half_window_volatility_ratio":vol_ratio,
             "extreme_2sigma":stretched,"definition":"rolling_20_close_zscore_and_two_half_drift_diagnostic_v1"},
            ("This is not a formal stationarity test; structural breaks can make historical means irrelevant.",)
        )]
