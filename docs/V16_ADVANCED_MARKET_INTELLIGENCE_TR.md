# IMPERION FINANCE V16 – Advanced Market Intelligence

## V16'da gelenler

1. **SMT → Director / Playbook:** BTC-ETH, SOL-ETH, BNB-ETH, XRP-BTC ve ADA-BTC eşleşmeleri. Eş varlık yapı verisi aynı setup timeframe'den alınır. Veri yoksa veya zaman hizası tutmuyorsa SMT `unavailable` kalır; nötr varsayılmaz. Yön uyumlu SMT en fazla +0.10, ters SMT -0.08 modifier uygular.
2. **Killzone / overlap:** Europe/London 07:00–10:00 ve America/New_York 08:30–11:00 yerel saatleri `zoneinfo` ile hesaplanır. Yaz/kış saati otomatik izlenir. Gerçek UTC kesişimi overlap olarak etiketlenir. Session sweep ile birlikte olduğunda sınırlı skor modifier uygulanır.
3. **Internal / external structure:** External yapı standart swing ölçeğinde, internal yapı bağımsız minor swing ölçeğinde hesaplanır. İkisi PAMap summary ve Director intelligence raporuna yazılır.
4. **Displacement quality:** Mum gövdesi/aralık, ATR katı, son 20 mumun hacmine göre hacim proxy'si ve önceki üç mumdaki yön devamlılığı 0–1 skorunda birleştirilir.
5. **Triple confluence:** OTE aktifken aynı yöne ait kaliteli IDM ve fiyata yakın mitigation zone aranır. Tek bir coherent setup varsa +0.15'e kadar modifier verilir.
6. **Session CISD:** Sweep quality >= 0.65 ve CISD follow-through >= 0.60 zorunludur. Momentum/trend hizası yoksa skor azaltılır. Overlap/killzone ayrıca sınırlı şekilde dikkate alınır.
7. **Order Flow:** AggTrade flow_profile delta ratio playbook yönüne göre +0.06 / -0.06 modifier uygular. Ayrıca candle taker-buy delta üzerinden fiyat/delta divergence proxy hesaplanır; sweep ile aynı yöndeyse sınırlı +0.04 modifier uygulanır. Proxy, tick-level CVD olarak sunulmaz. Akış verisi yoksa skor değiştirilmez.
8. **Adaptive thresholds:** Yüksek volatilitede IDM/displacement kalite eşikleri sıkılaşır; OTE toleransı daralır. Bunlar yalnızca analiz eşikleridir; risk limitlerini gevşetmez.
9. **Mean threshold:** OTE ve Mitigation planlarında zone 50% seviyesi equilibrium referansı olarak raporlanır; mevcut execution akışında market entry otomatik olarak limit emre çevrilmez.
10. **MMBM/MMSM research candidate:** Dar birikim aralığı + sweep + displacement adayını açıklar. `research_only=True`; doğrudan playbook veya emir üretmez.

## Skor ilkeleri

- Playbook score 0..1 aralığında clamp edilir.
- SMT, flow ve session modifier'ları olasılık veya win-rate değildir.
- Aynı kanıtı birden fazla katmanda sınırsız saymamak için modifier'lar bounded tutulur.
- Eksik korelasyon/flow verisi veto değildir; unavailable olarak raporlanır.
- MMBM/MMSM yalnızca araştırma etiketi taşır.

## Veri ve zaman varsayımları

- SMT için aynı sembol çifti ve aynı setup timeframe gerekir. Son mum zamanları bir mumdan fazla ayrışıyorsa sinyal kullanılamaz.
- Killzone'lar IANA timezone verisiyle hesaplanır. Container imajında timezone database bulunmalıdır; Python 3.11+ zoneinfo kullanılır.
- Binance perpetual sembol eşleşmeleri yapılandırılmış allowlist ile sınırlıdır; otomatik korelasyon keşfi yapılmaz.

## Doğrulama

Yeni testler: killzone DST dönüşümü, internal/external yapı alanlarının hesaplanması, displacement quality sınırları, SMT modifier yönü, triple confluence ve MMBM research-only davranışı. Tam doğrulama için GitHub Actions matrix, Docker CI ve Binance Testnet dry-run adımları çalıştırılmalıdır.

## Güvenli dağıtım

Varsayılan `MODE=dry` korunur. Önce GitHub CI ve Docker build, ardından cloud health-check, daha sonra Binance Testnet. Canlı emir için mevcut live gate, testnet kanıtı ve manuel operatör onayı gereklidir.

## Öğrenme verisi uyumluluğu

V16 `CORE_VERSION=agents-1-v16-market-intelligence` kullanır. V15 shadow/learning kayıtları otomatik olarak V16 karar ağırlıklarına karıştırılmaz; eski kayıtlar veritabanında korunur ve ayrı geçmiş veri olarak kalır.
