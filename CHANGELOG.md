# Changelog

## 17.0.0 — Unified Institutional Intelligence
- Unified V16.1 market intelligence as the primary architecture.
- Preserved V14 hardened execution, fail-closed risk controls, signed Testnet evidence and operator-bound Live gate.
- Preserved V15/V16 SMC-ICT, Order Flow, CVD, delta, volume profile, SMT, IDM, OTE, mitigation, CISD and session playbooks.
- Retained V16 confluence, displacement quality, adaptive thresholds, MMBM/MMSM research candidates, toxicity guard and portfolio inventory controls.
- Added V14 production runbook and release-bundle verification utility.
- Standardized release metadata at 17.0.0. Live trading remains disabled until fresh Testnet evidence and manual approval are provided.

## 16.1.0 — Unified Crypto Market Intelligence
- Reintroduced V15.2-inspired inventory concentration controls and connected them to the runtime decision path.
- Added direction-aware toxicity scoring from aggTrade delta, stacked trade imbalances, fresh order-book depth and spread.
- Added inventory-based risk scaling to position sizing; the multiplier can only reduce risk, never increase it.
- Added fail-closed inventory limit and toxicity veto before account sizing / order execution.
- Aligned direct `Settings()` defaults with `load_settings()` conservative risk profile to prevent divergent defaults.
- Added focused regression tests for portfolio guards and missing-data behavior.
- Retained V16 SMC/ICT, SMT, session, displacement, confluence and market-model intelligence.

# Changelog

## 16.0.0
- Advanced Market Intelligence: SMT artık Director karar zincirine bağlı; yalnızca eşleştirilmiş ve zaman hizalı sembol verisi varsa kullanılır.
- DST-aware London Open, New York AM ve gerçek London/NY overlap killzone etiketleri eklendi.
- Internal ve external structure bağımsız swing ölçeklerinde hesaplanıp raporlanır; advanced IDM/Session-CISD modelleri external sweep + trigger-TF internal CHoCH ister.
- Displacement quality; body/range, ATR multiple, volume proxy ve ardışık yön devamlılığını birleştirir.
- OTE + IDM + Mitigation yakınlık confluence katmanı eklendi; kanıtlar üst üste sayılmaz ve skor 0..1 aralığında sınırlandırılır.
- Session CISD filtreleri sweep quality >= 0.65 ve follow-through >= 0.60 olacak şekilde sıkılaştırıldı.
- AggTrade CVD/delta, candle-delta divergence proxy, SMT ve session killzone teyitleri playbook skoruna sınırlı ve açıklanabilir modifier olarak bağlandı.
- OTE/Mitigation planlarına 50% zone equilibrium referansı eklendi; market execution davranışı otomatik limit emre çevrilmedi.
- Bounded volatility-adaptive thresholds ve araştırma amaçlı MMBM/MMSM aday tespitçisi eklendi; MMBM/MMSM tek başına emir sinyali değildir.
- Learning/shadow model core etiketi değiştirildi; V15 skor semantiğine ait eski öğrenme verisi V16 karar ağırlıklarına karıştırılmaz.
- Playbook açıklamalarına SMT, Order Flow, displacement ve killzone kanıtları eklendi.


## 15.0.0
- Birleşik temel: V14.2.1 Hardened runtime, Binance execution safety, Guardian, risk gates, learning ve research araçları korundu.
- Advanced PA: IDM, OTE, Mitigation Block, Session Liquidity, CISD ve dört yeni playbook entegre edildi.
- CVD, Delta Ratio, fiyat seviyeli trade-flow profile, POC ve SMT modülleri geri korundu.
- Mitigation zone açıklama/formatlama hatası düzeltildi.
- Session Liquidity günlük UTC seanslarına göre ayrıştırıldı; günler arası mum birleştirme engellendi.
- IDM sweep tespiti ana sweep mumundan kesin olarak önce gerçekleşecek şekilde sınırlandı.
- Muhafazakâr risk varsayılanları: işlem başına %0,25, tek açık pozisyon, 2x azami kaldıraç, %5 maksimum drawdown.
- Release metadata ve cloud deployment dokümantasyonu V15.0.0 ile eşitlendi.

## 14.2.1
- Hardened execution, testnet-to-live gate, Guardian ve risk altyapısı.
