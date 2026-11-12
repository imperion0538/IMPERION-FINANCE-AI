IMPERION FINANCE AI — Radar işlem evrenine kabul
Bu değişiklik V17'nin mevcut işlem motorunu kullanır. Radar taraması kendi başına emir göndermez. Yeni sembol izni; risk, AI, veri kalitesi, hesap, preflight, canlı kapısı, stop ve likidasyon denetimlerinin yerine geçmez.
Varsayılan davranış
RADAR_TRADING_ENABLED=false: eski tarama davranışı sürer. Sabit SYMBOLS listesi değiştirilmez. İlk dinamik uygulama yalnızca day stilini destekler; day aktif değilse etkinleştirme reddedilir.
Gerekli koşullar
Operatör RADAR_TRADE_ALLOWLIST içine sembolü açıkça eklemeli.
Coin güncel Radar adayları arasında, 1h filtrelerinden geçmiş olmalı.
Son 24 saat quote hacmi RADAR_MIN_QUOTE_VOLUME eşiğini sağlamalı.
Coin için derin Director analizi onaylı, yönü LONG/SHORT ve setup'ı mevcut olmalı.
İşlem için gereken mum geçmişi, güncellik, boşluksuz veri ve yapılandırılmış stream sağlık kontrolleri geçmeli.
Son exchangeInfo içinde işlem gören perpetual kontrat olmalı; filtre kaydı güncel olmalı.
Eşzamanlı dinamik sembol sayısı ve süreli kabul koşulları geçmeli.
Bu şartlar yalnızca sembole yeni giriş değerlendirme izni verir. İşlem, yeni analiz ve standart risk kapılarından sonra açılabilir. Gerçekleşmemiş/eksik veriden izin üretilmez.
Ortam değişkenleri
Anahtar
Varsayılan
Açıklama
RADAR_TRADING_ENABLED
false
Dinamik girişleri etkinleştirme
RADAR_TRADE_ALLOWLIST
boş
Operatörün değerlendirmeye izin verdiği USDT sembolleri
RADAR_MAX_ACTIVE
3
Aynı anda kabul edilen dinamik sembol tavanı; pozisyon limiti değildir
RADAR_MIN_QUOTE_VOLUME
10000000
24 saatlik minimum USDT quote hacmi
RADAR_ADMISSION_TTL_SECONDS
1200
Kabul süresi; 60–1800 saniye
Test örneği (coin önerisi değildir):
MODE=dry
ACTIVE_STYLES=day
RADAR_COUNT=30
RADAR_DEEP_COUNT=5
RADAR_TRADING_ENABLED=true
RADAR_TRADE_ALLOWLIST=INJUSDT,FILUSDT
RADAR_MAX_ACTIVE=2
RADAR_MIN_QUOTE_VOLUME=10000000
RADAR_ADMISSION_TTL_SECONDS=1200
Allowlist tek başına işlem açmaz. Seçilen coin ilk derin taramaya girmeyebilir; güncel veri ve Director onayı gelene kadar kabul edilmez. Küçük sermayede borsa minimum emir büyüklüğü planlanan riski aşıyorsa işlem reddedilir; bu engeli aşmak için risk büyütülmez.
Çalışma sırası ve iptal
Radar adayları yaklaşık 10 dakikada, derin tarama yaklaşık 15 dakikada yenilenir. Etkin modda Radar yenilenirken borsa filtreleri de yenilenir. Tarama gecikirse izin süresi dolar; bu sırada yeni giriş kapanması beklenen davranıştır. Başarısız bir derin tarama sonrasında eski izin yeniden kullanılmaz.
Analizden sonra, seri giriş kilidi içinde ve hesap/bracket/boyutlandırma çağrılarından sonra emirden hemen önce Radar kabulü tekrar kontrol edilir. Veri/quote sağlığı yeniden doğrulanır. Portföy yoğunlaşma çarpanı girişte tekrar hesaplanır ve önceki çarpandan yukarı çıkarılmaz.
Sembol listeden düşer veya süre dolarsa yeni giriş kapanır. Veritabanında kayıtlı açık sistem pozisyonu için mum ve WebSocket izleme devam eder; stoplar iptal edilmez. Süreli giriş izinleri yeniden başlatmada geri yüklenmez. Açık pozisyonların kalıcı veritabanından takibi devam eder.
Gözlem ve entegrasyon
Kimlik doğrulamalı /status yanıtındaki execution_universe alanı sabit listeyi, güncel Radar izinlerini, ret nedenlerini ve izlenen sembolleri gösterir. HTTP TradingView kabulü ve Telegram /analiz güncel giriş listesini kullanır. TradingView bir ipucudur; süre dolmuş bir aday kuyrukta bekledikten sonra yeniden yetkilendirilemez. Dinamik girişler yalnızca day stilindedir.
Tarama Telegram mesajı hâlâ tarama etiketi taşır. Normal işlem/sinyal bildirimi ayrı mevcut akıştan gelir.
Bilinen sınırlar
Allowlist ortam değişkenidir; değiştirmek dağıtım/yeniden başlatma gerektirir. Yeni bir Telegram emir yetkilendirme komutu eklenmedi.
Kod varsayılanı hâlen config.py içindeki 16 sabit semboldür. inventory.py içindeki 13 elemanlı referans kümesi bir işlem yetkisi listesi değildir. Bu paket sabit coin seçimini sessizce değiştirmez.
Gerçek Binance Demo, gerçek canlı API ve bulut üzerinde uçtan uca doğrulama bu çalışmada yapılmadı. Testler deterministik FakeBinance ile çalışır.
İşlem modeli başarısı veya kârlılık kanıtlanmış değildir. Ağırlıklar ve risk limitleri değiştirilmedi.
