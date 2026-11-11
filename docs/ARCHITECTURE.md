# Mimari (v14.2.1)

## Paketler

| Paket | Sorumluluk |
|---|---|
| `config.py` | Ortam değişkenlerinden `Settings`; stiller (`STYLES`); canlı kapı (`orders_possible`); sürüm tek kaynak. |
| `exchange/` | `rest.py` (HMAC imza, saat senkronu, 429/418 koruması, ortak `RateGuard`), `ws.py` (abonelik, yeniden bağlanma), `user_stream.py` (özel akış, guardian hızlandırıcı), `filters.py` (tick/step/min notional). |
| `data/` | `candles.py` (REST kapanış mumları, taker-buy hacmi), `microstructure.py` (işlem akışı, balina eşiği, footprint, defter), `health.py` (veri sağlığı + son veri kalitesi kapısı), `universe.py` (tarama listesi, fonlama). |
| `analysis/priceaction/` | `structure.py` (salınımlar, BOS/CHoCH durum makinesi, işlem aralığı), `liquidity.py` (havuzlar, süpürmeler), `zones.py` (OB/FVG → breaker/IFVG), `candles.py` (formasyonlar), `mapper.py` (`build_map` → `PAMap`), `playbooks.py` (4 kurulum modeli). |
| `analysis/` | `market.py` (rejim, volatilite, momentum, seans, genişlik), `mtf.py` (8 TF görünümü), `orderflow.py` (mum akışı + canlı akış), `indicators.py`. |
| `agents/` | 11 ajan; her biri `AgentReport` (yön, güven, özet, gerçekler, veto) döndürür, hata verirse "kullanılamaz" olur. |
| `core/` | `director.py` (müdür: karar zinciri), `policy.py` (ağırlıklar, eşikler, rejim profilleri), `consensus.py`, `planner.py` (plan, kalite, derece). |
| `risk/manager.py` | Kalıcı limitler, kill switch, günlük kayıp / drawdown durdurma, korelasyon koruması, borsa filtrelerine duyarlı boyutlandırma, likidasyon tamponu. |
| `execution/` | `binance.py` (gerçek yürütme, Algo Service koşullu emirleri, maker giriş, fonlama geliri), `sim.py` (dry), `lifecycle.py` (giriş → stop → TP → başa-baş → izleyen stop → uzlaştırma). |
| `learning/` | `shadow.py` (gölge işlemler), `model.py` (kazanma olasılığı, walk-forward), `context.py` (negatif bağlam filtresi), `learner.py` (ağırlık önerileri), `service.py` (çalışma zamanı görüşü + öğrenme döngüsü). |
| `notify/telegram.py`, `reports/daily.py` | Telegram kuyruğu, mesaj biçimleri; günlük rapor (metin + HTML, SMTP). |
| `runtime/` | `engine.py` (her şeyi bağlar; thread'ler), `telegram_service.py` (sinyal, olay, özet, komutlar). |
| `server/` | `http.py` (/health, /status, /admin/*, webhook), `dashboard.py`, `webhook.py` (TradingView). |
| `gate.py`, `testnet.py` | Testnet → Live kapısı (HMAC-SHA256 imzalı kanıt + kanıt özetine bağlı elle onay; imza her değerlendirmede yeniden doğrulanır) ve test edilebilir Testnet doğrulayıcısı (One-way mod, emir yaşam döngüsü, gerçek `liquidationPrice` ve likidasyon modeli karşılaştırması). |
| `risk/liquidation.py` | Kaldıraç kademeleri ve izole likidasyon fiyatı. |
| `analysis/radar.py` | Tarama listesinin 1. kademe puanlaması ve 2. kademe seçimi. |
| `research/sensitivity.py` | Eşik hassasiyeti (teşhis). |
| `research/backtest.py` | Geçmişi gerçek müdürle ileriye bakmadan oynatan backtest. |
| `fakes.py`, `selftest.py` | Katı sahte Binance + yürütme güvenliği senaryoları. |

## Karar akışı (`Engine.evaluate`)

1. **Veri sağlığı** — mum geçmişi/tazeliği/boşluk, WebSocket, işlem ve defter tazeliği, spread. Sağlıksızsa
   `DATA_BLOCKED` (gölge kaydı da yapılmaz).
2. **Müdür analizi** (`Director.analyze`) — kurulum ve tetik TF price action haritaları, 8 TF görünümü, rejim; 11 ajan
   raporu; kurulum modelleri; her kurulum için tamlık → üst TF trendi → vetolar → plan → uzlaşı → derece.
3. Bir plan varsa **gölge işlem** kaydı (onaylı olsun olmasın; öğrenme verisi).
4. Analiz onaylıysa: **hesap riski** (`precheck`), **son veri kalitesi** (gecikme, kapanmış mum, spread),
   **yapay zekâ görüşü** (bağlam filtresi + doğrulanmış model; yalnızca engelleyebilir).
5. `can_trade` (gözlem modu / preflight / canlı kapısı / durdurma) → işlem kilidi altında yeniden `precheck` + `size` →
   `Lifecycle.open_trade`.
6. Telegram: onaylı (ya da `setups` düzeyinde analizce geçerli) ve derecesi yeterli sinyal yayınlanır.

## İşlem yaşam döngüsü ve değişmezler

- Giriş `MARKET` (ya da maker modunda post-only `LIMIT`); zaman aşımında emir istemci kimliğiyle sorgulanır.
- **Pozisyon stopsuz kalamaz:** `STOP_MARKET` (`closePosition`, `MARK_PRICE`) 3 deneme; olmazsa piyasa emriyle kapat,
  kapanmazsa sistem durur ve kritik uyarı verir.
- TP1 / TP2 `TAKE_PROFIT_MARKET reduceOnly`. TP1 dolunca stop başa-başa, sonra ATR katı mesafeyle izleyen stop; yeni
  stop önce konur, eskisi sonra iptal edilir.
- Giriş sonrası ve her guardian turunda borsanın `liquidationPrice` değeri stopla karşılaştırılır; stopun içindeyse pozisyon kapatılır.
- `manage()` (4 sn ya da kullanıcı akışı olayında hemen): pozisyon yoksa yetim emirleri iptal, `userTrades` ve fonlama
  geliriyle PnL/R kesinleştir. Pozisyonlar okunamazsa yeni girişler engellenir, 60 sn sürerse sistem durur.
- Yeniden başlatma: `PENDING` / `ENTRY_UNKNOWN` kayıtlar borsada pozisyon varsa devralınır, yoksa kapatılır. Sistemin
  açmadığı pozisyonlara asla dokunulmaz.

## Kalıcı veri (SQLite, `/data/imperion.db`)

`kv` (risk durumu, imleçler, model) · `events` · `decisions` (ajan raporları, kurulum, gerekçeler) · `trades` ·
`shadows` (çekirdek sürümü etiketli) · `equity` · `webhook_queue` · `ai_weights` · `reports`. Eski veritabanları
eklemeli ve tekrarlanabilir biçimde güncellenir; eski motorların öğrenme verisi bu çekirdeği eğitmez.
