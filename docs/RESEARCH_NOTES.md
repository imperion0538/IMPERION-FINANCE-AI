# Araştırma notları ve API dayanakları

Bu sistemin dayandığı Binance USDⓈ-M Futures davranışları (resmi dokümantasyon: *developers.binance.com → Derivatives → USDS-Margined Futures*; ilk kez v8 döneminde kontrol edildi; v13'te bu ortamdan Binance belgelerine erişilemediği için yeniden doğrulanamadı).
**Binance bunları değiştirebilir** — canlıya almadan önce aşağıdaki "doğrulanmamış" maddeleri ilk saatlerde izleyin.

## Doğrulanmış (resmi sayfalarda görüldü)

| Konu | Not |
|---|---|
| Koşullu emirler | `STOP_MARKET`, `TAKE_PROFIT_MARKET` vb. **Algo Service** üzerinden: `POST /fapi/v1/algoOrder` (`algoType=CONDITIONAL`, `symbol`, `side`, `type`, `triggerPrice`, `workingType`, `clientAlgoId`, `closePosition` **veya** `quantity`+`reduceOnly`). `closePosition`, `quantity`/`reduceOnly` ile birlikte gönderilemez. `clientAlgoId` deseni `^[\.A-Z\:/a-z0-9_-]{1,36}$`. Maliyet: 1 IP ağırlığı + emir-hızı limitleri. |
| Açık algo emirleri | `GET /fapi/v1/openAlgoOrders` (symbol opsiyonel) → **düz JSON dizisi**; alanlar `algoId, clientAlgoId, orderType, triggerPrice, quantity, closePosition(bool), reduceOnly(bool), algoStatus…` (fiyat/miktar string). |
| Eski uç | Koşullu tipler `/fapi/v1/order`'da reddedilir (`-4120`); piyasa emri hâlâ `/fapi/v1/order`. |
| Algo iptali | `DELETE /fapi/v1/algoOrder` (algoId), `DELETE /fapi/v1/algoOpenOrders` (symbol). |
| WebSocket | `wss://fstream.binance.com/market/stream` (aggTrade, kline…) ve `/public/stream` (depth, bookTicker); `SUBSCRIBE` JSON mesajlarıyla. Bağlantı 24 saatte düşer → 23 saatte yenilenir. Demo: `wss://demo-fstream.binance.com`. |
| Demo/testnet | REST `https://demo-fapi.binance.com`. |
| Anahtar izinleri | `GET /sapi/v1/account/apiRestrictions` (`https://api.binance.com`) → `enableWithdrawals, enableFutures, ipRestrict`. |
| Hesap | `/fapi/v3/account`, `/fapi/v3/positionRisk` (404'te v2'ye düşülür). |
| Hız limitleri | 429 = yavaşla; 418 = IP yasağı (tüm istekleri durdur); kullanılan ağırlık `X-MBX-USED-WEIGHT-1M` başlığında. |
| İmza | Sorgu dizesinin HMAC-SHA256'sı; `timestamp` + `recvWindow`; başlık `X-MBX-APIKEY`; `-1021` → saat senkronu. |
| `userTrades` | En fazla 7 günlük pencere → uzun işlemlerde dilimlenir. |

## Doğrulanmamış / savunmacı kodlananlar

- **Aynı yönde ikinci `closePosition` emri:** klasik API `-4130` ile reddediyordu; Algo dokümanı bundan söz etmiyor. Kod ikisini de tolere eder (yeni stop önce, olmazsa `quantity+reduceOnly` yedeği, sonra eskiyi iptal).
- **Üçüncü taraf sayfalar** `closePosition`'ın algo ucunda desteklenmediğini iddia ediyordu; resmi sayfa destekliyor. Kod önce `closePosition`, reddedilirse `quantity+reduceOnly` dener.
- **Gerçek `-2021` ("Order would immediately trigger")** davranışı: giriş sonrası fiyat stopu geçtiyse stop reddedilir → sistem pozisyonu anında kapatır (tasarım gereği).
- Demo (testnet) ortamında algo uçlarının tam eşdeğer davranıp davranmadığı doğrulanmadı.

## Tasarım kararlarının gerekçesi

- **Mumlar REST'ten:** `taker buy base volume` her zaman diliminde (geçmiş dahil) delta verir; WS'ten mum biriktirmek yeniden bağlanmada boşluk üretir. WS yalnızca işlem akışı ve defter içindir.
- **Kurulum önce, uzlaşı sonra (v13):** oylama tek başına işlem açamaz; önce tamamlanmış bir price action kurulumu (süpürme, geri çekilme, kırılım-test, aralık kenarı) gerekir, ajan uzlaşısı onu onaylar. Her ajan ayrı bir bilgi kaynağıdır (yapı, likidite, bölgeler, akış, momentum…); uzlaşıda en az bir price action ajanının katılması zorunludur.
- **Gölge-öncelikli öğrenme:** 100 USDT'lik hesap ayda birkaç işlem açar; istatistik için oluşturulan her kurulum izlenir. Canlı ağırlıklar yalnızca onayla değişir; sınırlar (±%10 adım, 0.5–1.5×), büzüştürme (K=20) ve kronolojik doğrulama aşırı uyumu sınırlar.
- **Küçük hesapta boyut:** borsa minimumu riski şişiriyorsa işlem **atlanır** (`MAX_RISK_OVERSHOOT=1.25`).
- **Balina verisi:** herkese açık işlem akışından büyük agresif işlemler (uyarlanır eşik). Kimlik değil *baskı* ölçer; spoofing ve parçalı emirler nedeniyle tek başına karar vermez (mikro-yapı ailesinde diğer kanıtlarla birlikte sayılır).

## Kapsam dışı (bilinçli)

Otomatik parametre optimizasyonu (bilinçli olarak yok: aşırı uydurma riski), çoklu borsa, spot, opsiyon, hedge modu, Multi-Assets modu.
