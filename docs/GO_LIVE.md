# Canlıya geçiş kontrol listesi (v14.2.1)

Sırayı bozmayın; her adım bir öncekine güvenir. API anahtarlarını yalnızca sunucunun ortam değişkenlerine girin
(Railway → Variables); sohbete, GitHub'a, e-postaya yazmayın.

## A. Binance hesabı

- [ ] USDⓈ-M **Futures** hesabı açık (ABD IP'leri engellidir).
- [ ] **Position Mode = One-way**, **Asset Mode = Single-Asset** (preflight yanlış ayarda çalışmayı reddeder).
- [ ] Bu sistem için **ayrı bir alt hesap**, cüzdanda ≈ `TRADING_CAPITAL_USDT` kadar USDT. Elle işlem ya da para
      transferi risk referanslarını bozar.
- [ ] API anahtarı: Reading + Futures açık, **Withdrawals KAPALI** (açıksa sistem çalışmaz), mümkünse IP kısıtlı.

## B. Sunucu (Railway örneği)

- [ ] Özel GitHub deposu (`.env` yüklenmez), Dockerfile ile dağıtım, bölge Avrupa/Asya (ABD = HTTP 451).
- [ ] **Volume → `/data`** (yoksa her dağıtımda risk limitleri, işlemler ve öğrenme verisi sıfırlanır).
- [ ] **Replica = 1** (iki kopya aynı hesapta çift işlem açar).
- [ ] Değişkenler: `.env.example`. İlk aşamada `MODE=dry`, `LIVE_TRADING_CONFIRM` boş, Telegram ve `ADMIN_TOKEN` dolu.

## C. Gözlem (dry) — en az 1–2 hafta

- [ ] `/health` → `ready: true`, `ws_healthy: true`; logda `candle bootstrap: 128/128 series` (16 sembol × 8 TF).
- [ ] Telegram başlangıç mesajı geldi; `/durum`, `/analiz SOLUSDT day`, `/piyasa` cevap veriyor; başka bir sohbetten
      gönderilen komut yok sayılıyor.
- [ ] `/dashboard` → `ADMIN_TOKEN` ile açılıyor, son kararlar ve gerekçeleri görünüyor.
- [ ] Gelen sinyalleri grafikte kendiniz değerlendirin: kurulum adımları, stop ve hedefler mantıklı mı?
- [ ] `python -m imperion backtest --symbol <SEMBOL> --style <stil> --days 30` ile her stil için hunıyı ve en sık ret
      nedenlerini okuyun. Sinyal hiç yoksa ya da çok fazlaysa nedenini bu rapor gösterir.
- [ ] `AI_MIN_SAMPLES` kadar gölge işlem çözüldükten sonra `python -m imperion ai-status`: model `validated` mı,
      en kötü / en iyi bağlamlar neler?

## D. Market-signal research (read-only)

- [ ] `python scripts/market_validation.py --symbol BTCUSDT --comparison ETHUSDT --timeframe 5m --limit 1000 --horizon 10` ile BOS/CHoCH, sweep, OB/Breaker, FVG/IFVG ve SMT sinyal raporunu üretin.
- [ ] Her sinyal sınıfında örneklem sayısı, hit rate, forward return, MFE ve MAE değerlerini inceleyin. Bu rapor ücret/kayma/fonlama içermez; tek başına canlı işlem onayı değildir.
- [ ] En az 3 farklı piyasa rejiminde ve birden fazla sembolde tekrar edin; parametreleri test dönemine bakarak ayarlayıp aynı dönemi tekrar doğrulama seti gibi kullanmayın.
- [ ] Canlı order flow `aggTrade` WebSocket akışından; candle delta ise REST kline taker-buy volume alanından gelir. `/status` ve analiz raporlarında ikisini ayrı değerlendirin.
- [ ] Gerçek trade akışını emir göndermeden ölçmek için `python scripts/orderflow_capture.py --symbol BTCUSDT --seconds 300` çalıştırın. `aggtrade_events`, `cvd`, `delta_ratio`, POC ve footprint seviyelerini raporlayın; WebSocket kesintisi veya sıfır veri durumunu başarısız doğrulama sayın.
- [ ] CVD/footprint warm-up tamamlanmadan order-flow katmanının unavailable olmasını bekleyin; stale stream ile karar üretmeyin.

## E. Testnet

- [ ] Demo hesabı **One-way** pozisyon modunda ve **Single-Asset** marj modunda (Multi-Assets kapalı).
- [ ] `TESTNET_EVIDENCE_SECRET` üretildi (ör. `python -c "import secrets; print(secrets.token_hex(32))"`), GitHub secret'ına
      ve canlı sunucunun ortamına **aynı değer** yazıldı; başka hiçbir yerde saklanmıyor.
- [ ] `MODE=testnet` + Futures Demo anahtarları → `python -m imperion testnet-check` (salt-okunur; `position_mode` `PASS`).
- [ ] Emir yaşam döngüsü: `TESTNET_ORDER_SMOKE=true TESTNET_ONLY_ACK=I_ACCEPT_TESTNET_ORDERS TESTNET_ORDER_QTY=<küçük>
      python -m imperion testnet-check` → `order_lifecycle` ve `exchange_liquidation` `PASS`
      (`exchange_liq`, `model_liq`, `stop_to_liq`, `model_optimism` değerlerini not edin).
- [ ] Sistemin kendi açtığı bir işlemde: pozisyon + bir STOP_MARKET (closePosition) + iki TAKE_PROFIT_MARKET emri
      Binance'te görünüyor; TP1 sonrası stop başa-başa ve sonra izleyen stopa geçiyor; kapanışta fonlama PnL'e giriyor.
- [ ] Yeniden başlatma: açık pozisyon varken konteyneri yeniden başlatın → pozisyon devralınıyor, stop yerinde.
- [ ] `ENTRY_MODE=maker` kullanacaksanız dolan, kısmi dolan ve dolmayan girişlerin her biri tek stop ile bitiyor.
- [ ] `USER_STREAM=true` kullanacaksanız WebSocket yolunu güncel Binance belgesinden doğrulayın; `/status` →
      `user_stream.connected: true`.

- [ ] Emir testi geçen `testnet-check` çıktısında `signed gate evidence recorded` ve `EVIDENCE_SHA256=...` görünüyor
      (aynı `/data` biriminde). Kanıt GitHub `testnet` iş akışından geldiyse: artifact'ı indirin,
      `python -m imperion verify-evidence testnet-evidence.json` → `EVIDENCE=VALID`, ardından
      `python -m imperion import-evidence testnet-evidence.json`.
- [ ] `python -m imperion sensitivity --symbol <SEMBOL> --style <stil> --days 30` → `STABLE` mı, yoksa sonuçlar
      eşiklere çok mu bağlı?

## F. Canlı

- [ ] `python -m imperion approve-live --operator ADINIZ --confirm I_APPROVE_LIVE_TRADING` → `gate-status` `ok: true`,
      `evidence.signed: true`, `evidence.position_mode: ONE_WAY`.
- [ ] `python -m imperion preflight` → `RESULT: OK`.
- [ ] `MODE=live`, `LIVE_TRADING_CONFIRM=I_ACCEPT_REAL_MONEY_RISK`. İlk hafta her gün: Telegram olayları, `status`,
      Binance'teki pozisyonlarla `open_trades` eşleşmesi.

## G. Acil durum

| Durum | Eylem |
|---|---|
| Yeni işlem açılmasın | Telegram `/durdur`, `POST /admin/halt`, `KILL_SWITCH=true` ya da `python -m imperion halt` |
| Her şeyi kapat | `POST /admin/flatten` (yalnızca sistemin açtığı pozisyonlar) — gerekirse Binance'ten elle |
| "stop konamadı" / "EMERGENCY CLOSE FAILED" uyarısı | Binance'te pozisyonları hemen elle kontrol edin; sistem kendini durdurmuştur |
| API anahtarı sızdı şüphesi | Binance'ten anahtarı silin, yenisini oluşturun, değişkenleri güncelleyin |

## H. Gerçekçi beklenti

- 100 USDT'de işlem başına risk ≈ 1 USDT; ücret + kayma ≈ notional'ın %0.16'sı. Sonuçlar küçük ve gürültülüdür;
  birkaç düzine işlemden önce hiçbir istatistik anlamlı değildir.
- BTCUSDT çoğu zaman borsa minimum işlem değeri nedeniyle atlanır (normal).
- Sistem bilerek seçicidir. Kaldıraçlı vadeli işlem tüm sermayeyi kaybetme riski taşır; kâr garantisi yoktur.
