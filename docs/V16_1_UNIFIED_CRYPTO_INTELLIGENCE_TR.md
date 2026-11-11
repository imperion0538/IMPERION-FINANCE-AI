# IMPERION FINANCE V16.1 — Unified Crypto Market Intelligence

## Amaç
V16.1, V16 Advanced Market Intelligence çekirdeğini korurken V15.2'de bulunan Inventory ve Toxicity yaklaşımını gerçek Runtime karar zincirine bağlar. Bu sürüm, Binance USDⓈ-M perpetual kripto piyasaları içindir.

## V16.1'de uygulanan değişiklikler

### 1. Inventory Guard
- Runtime `Lifecycle.exposure()` / `Database.open_trades()` çıktısını kullanır.
- Net yönsel maruziyet, sembol bazlı notional ve BTC/ETH/SOL/BNB major kümesi yoğunluğu hesaplanır.
- Yoğunlaşma arttığında `size_multiplier` 1.0'ın altına iner.
- `RiskManager.size(..., risk_multiplier=...)` risk bütçesini yalnızca azaltır; artırmaz.
- Aynı sembole tekrar giriş, cooldown ve eşzamanlı pozisyon kuralları mevcut `RiskManager.precheck` tarafından yönetilmeye devam eder.

### 2. Toxicity Guard
- Son 5 dakikalık aggTrade delta ratio ve stacked imbalances kullanılır.
- Order-book imbalance yalnızca son 8 saniyede alınmış snapshot için hesaba katılır.
- Spread genişlemesi toxicity puanını artırır.
- LONG ve SHORT için ters yöndeki agresif akış / defter baskısı ayrı hesaplanır.
- 0.78 üzeri toxicity yeni girişi engeller; 0.90 ve üzeri extreme kabul edilir.
- Veri yokluğu teyit sayılmaz: eksik flow/book verisi tek başına veto üretmez.

#### 2.1 Tutarlı risk varsayılanları
- `Settings()` ve `load_settings()` varsayılanları aynı muhafazakâr profile hizalandı: %0,25 işlem riski, 1 açık pozisyon, %0,50 açık risk, %1 günlük yeni risk, %1,5 günlük zarar limiti, %5 maksimum drawdown ve 2x kaldıraç.

## 3. Karar zinciri
1. Director analiz ve playbook onayı.
2. Inventory yoğunlaşma değerlendirmesi.
3. RiskManager precheck, veri kalitesi ve öğrenme filtresi.
4. Toxicity veto.
5. Exchange filtreleri, liquidation kontrolü ve risk çarpanlı sizing.
6. Emir yürütme.

## Korunan V16 özellikleri
- Internal/external structure, SMT, IDM, OTE, Mitigation, CISD ve displacement kalite değerlendirmesi.
- Market model aday tespiti (research-only).
- Binance USDⓈ-M market data, execution lifecycle, kill switch ve live gate.
- Backtest, optimizer, walk-forward ve shadow tracking altyapısı.

## Sınırlar / henüz tamamlanmamış araştırma işleri
- Mevcut BookStore Binance partial-depth snapshot tutar; tam L2 order-book reconstruction, order-cancel/queue priority ve spoofing tespiti iddia edilmez.
- AggTrade delta ve mum delta, tam borsa footprint/cluster verisinin yerine geçmez.
- Funding ve open-interest verileri mevcut derivatives adapter kapsamındadır; her veri kaynağının zaman damgası ve eksikliği ayrı kontrol edilmelidir.
- Bu sürümde toxicity eşikleri başlangıç heuristics değerleridir; canlı örneklemle kalibre edilmemiştir.
- V16.1 için kârlılık veya pozitif expectancy iddiası yoktur. Out-of-sample ve Testnet doğrulaması yapılmadan canlıya geçilmemelidir.

## Güvenli kullanım
- Varsayılan `MODE=dry` korunur.
- API anahtarlarını ZIP'e veya GitHub deposuna koymayın; secrets / environment variables kullanın.
- Önce dry, sonra Binance Futures Demo/Testnet ve imzalı testnet evidence; canlı kapısı ayrı manuel onay gerektirir.
