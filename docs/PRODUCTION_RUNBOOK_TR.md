# IMPERION FINANCE 14.3.0 — Üretim ve canlıya geçiş kılavuzu

Bu paket Binance USDⓈ-M Futures içindir. Spot, MT5, Forex, altın veya endeks bağlantısı içermez.

## Önemli durum bildirimi

Bu ZIP; kaynak kodu, GitHub Actions iş akışları, Docker yapılandırması ve Railway dağıtım tanımını içerir. Ancak ZIP'in hazırlanması sırasında gerçek Binance Demo/Testnet hesabına emir gönderilmedi ve kullanıcının Railway projesine deploy yapılmadı. Dolayısıyla **canlı işlem güvenliği kanıtlanmış değildir**. Canlı emir kapısı varsayılan olarak kapalıdır. Bu kapıyı atlatmayın.

## Güvenli varsayılanlar

| Ayar | Varsayılan |
|---|---:|
| Mod | `dry` |
| İşlem başına risk | `%0,25` |
| Eşzamanlı pozisyon | `1` |
| Toplam açık risk | `%0,50` |
| Günlük yeni risk | `%1,00` |
| Günlük zarar sınırı | `%1,50` |
| Maksimum drawdown | `%5,00` |
| Kaldıraç / maksimum | `2x / 2x` |

Bunlar muhafazakâr başlangıç sınırlarıdır; kâr veya sermaye koruma garantisi değildir. Borsa minimum emir miktarı nedeniyle risk aşılıyorsa işlem reddedilmelidir; risk limitlerini yükselterek zorlamayın.

## A. GitHub'a yükleme

1. GitHub'da yeni ve boş bir repository oluşturun.
2. ZIP'i bilgisayarda/telefonda açın; ZIP dosyasının kendisini repo köküne yüklemeyin. İçindeki `README.md`, `imperion/`, `tests/`, `.github/`, `Dockerfile` ve diğer dosyalar repo kökünde olmalı.
3. `.env` dosyası, Binance anahtarları, Telegram token'ı, `storage/`, veritabanı veya Testnet kanıtı yüklemeyin. Yalnızca `.env.example` örnektir.
4. İlk commit'i `main` dalına gönderin.
5. GitHub → Actions bölümünde `ci` iş akışının tamamlanmasını bekleyin. Python 3.11/3.12/3.13 testleri, güvenlik taraması ve Docker build/healthcheck adımları yeşil olmadan ilerlemeyin.

## B. Railway kurulumu

1. Railway'de yeni proje → Deploy from GitHub Repo → bu repository.
2. Root Directory boş (repo kökü) kalmalı; Dockerfile otomatik algılanmalı.
3. Service → Settings → Deploy → tek replica/tek instance kullanın. Aynı Binance hesabında ikinci bot instance'ı çalıştırmayın.
4. Service → Volumes → `/data` mount path ile kalıcı volume ekleyin.
5. Variables bölümünde önce `MODE=dry`, `IMPERION_STORAGE_DIR=/data`, `PORT` için Railway'in verdiği değeri kullanın.
6. `ADMIN_TOKEN` ve `TRADINGVIEW_WEBHOOK_SECRET` için en az 32 karakterlik rastgele değerler belirleyin. İhtiyaç yoksa webhook entegrasyonunu açmayın.
7. İlk deploy'da Binance API anahtarları eklemeyin. `/health` 200 dönmeli, `/ready` ve loglar hatasız olmalı. Uygulama başlatılamadıysa healthcheck 503 vermelidir.
8. Deploy sonrası `/data` yazılabilirliğini ve redeploy sonrası SQLite verisinin korunduğunu doğrulayın.

## C. Dry çalışma — emir göndermez

En az 2 hafta `MODE=dry` çalıştırın. Piyasa verisi akışının taze olduğunu, sembol/zaman dilimi kapsamını, Telegram sinyallerini, reddedilme nedenlerini, stop/TP planlarını ve shadow/dry performans sınıflarının ayrı tutulduğunu kontrol edin. Bu aşamada hiçbir gerçek emir gönderilmemelidir.

## D. Binance USDⓈ-M Futures Demo/Testnet

1. Yalnızca Binance Demo/Futures Testnet API anahtarı oluşturun. Para çekme yetkisi hiçbir zaman açılmamalıdır.
2. GitHub repository → Settings → Environments bölümünde `testnet` ortamı oluşturun.
3. Secrets: `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET`, `TESTNET_EVIDENCE_SECRET` (en az 32 karakter).
4. Variables: `TESTNET_ORDER_QTY` (seçilen sembolün filtrelerine uygun küçük miktar), isteğe bağlı `TESTNET_SYMBOL=BTCUSDT`, `TESTNET_MAX_NOTIONAL_USDT=150`.
5. Actions → `testnet` → Run workflow. Bu akış Demo hesabında One-way/Single-Asset modunu, bağlantıyı, gerçek emir girişini, borsa tarafı stop'u, reduce-only kapanışı ve borsanın gerçek `liquidationPrice` değerini kontrol eder.
6. Yalnızca workflow tamamen başarılıysa imzalı `testnet-evidence` artifact'ını indirin. Kanıtın HMAC anahtarı canlı Railway değişkenindeki `TESTNET_EVIDENCE_SECRET` ile aynı olmalıdır.
7. Kanıtı güvenli bir yolla Railway'in `/data` alanına aktarın ve `python -m imperion import-evidence <dosya>` komutuyla içe alın. Kanıtı herkese açık repo/issue/chat içine koymayın.

## E. Canlı kapı — varsayılan kapalı

Canlı mod ancak Demo kanıtı geçerli ve 30 günden yeni ise, kanıt özeti operatör onayıyla eşleşiyorsa, preflight başarılıysa, kill switch kapalıysa ve API anahtar izinleri doğrulanmışsa açılabilir.

1. Önce ayrı bir Binance alt hesabı kullanın.
2. API anahtarında withdrawal kapalı, yalnızca gerekli Futures izinleri açık ve IP whitelist etkin olmalı.
3. Canlı Railway ortamına `TESTNET_EVIDENCE_SECRET`, imzalı Testnet kanıtı, `MODE=live` ve `LIVE_TRADING_CONFIRM=I_ACCEPT_REAL_MONEY_RISK` girilmeden canlı mod çalışmaz.
4. Operatör, `python -m imperion approve-live --operator <OPERATOR_ID> --confirm I_APPROVE_LIVE_TRADING` komutuyla onay verir. Onay kanıtın SHA-256 özetine bağlıdır.
5. `python -m imperion preflight` başarılı olmadan canlı emir açmayın. İlk canlı deneme ancak kaybetmeyi göze alabileceğiniz çok küçük tutarla, insan gözetiminde yapılmalıdır.
6. Herhangi bir stop doğrulama hatasında, veri gecikmesinde, bilinmeyen pozisyon maruziyetinde veya uzlaştırma hatasında sistemi durdurun ve pozisyonu Binance arayüzünden kontrol edin.

## F. Yayın ve geri dönüş

- GitHub Actions CI, security ve Docker kapıları yeşil olmalı.
- `release.yml` sürüm etiketi (`v14.3.0`) ile VERSION eşleşmesini, CI'ı, Testnet kanıtını ve production environment manuel onayını zorunlu tutar.
- Yeni sürüme geçmeden önce Railway `/data` yedeği alın. Geri dönüşte aynı SQLite dosyasını koruyun; eski sürümün şema uyumluluğunu doğrulamadan DB'yi değiştirmeyin.
- Canlı sorununda `KILL_SWITCH=true` veya Telegram `/durdur` kullanın; Binance açık emirleri/pozisyonları ayrıca doğrulayın.

## Canlıya geçiş kabul kriterleri

- [ ] Tam pytest paketi CI'da tamamlandı; başarısız veya atlanan test yok.
- [ ] pip-audit ve secret scan başarılı.
- [ ] Docker build, container healthcheck ve SIGTERM kapanışı başarılı.
- [ ] Railway tek replica + `/data` volume ile deploy edildi; restart sonrası veri korundu.
- [ ] En az 2 hafta dry çalışma gözlemlendi.
- [ ] Binance Demo/Testnet emir yaşam döngüsü ve likidasyon doğrulaması başarılı.
- [ ] İmzalı Testnet kanıtı canlı ortama aktarıldı ve imza doğrulandı.
- [ ] Operatör onayı verildi; preflight başarılı.
- [ ] İlk canlı işlem insan gözetiminde ve küçük riskle yapıldı.

Bir madde bile eksikse sistem canlı işlem için onaylanmış sayılmaz.
