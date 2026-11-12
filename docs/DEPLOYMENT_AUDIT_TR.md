V17 canlı kapıları ve bulut denetimi
İncelenen kaynak tabanı: df7fb14f92b2a95bdf4a748ce9ea699cdf3404a6.
Canlı işlem kapıları
Kaynakta ve mevcut testlerde aşağıdaki zincir korunur:
Varsayılan MODE=dry.
Gerçek emir için anahtarlar, doğru mod ve canlı doğrulama ifadesi gerekir.
Runtime preflight geçmeden emir açılamaz.
Canlı kapısı; HMAC ile imzalanmış, tam testnet kanıtı ve bu kanıtın hash'ine bağlı operatör onayı ister. Yeni kanıt önceki onayla aynı değildir; bozuk/eski kanıt kapıyı kapatır.
Hesap verisi, portföy/risk ön kontrolleri, AI veto, veri kalitesi ve toxicity engelleri emir öncesinde uygulanır.
Emir girişi kilit altında yeniden risk kontrolünden geçer. Bracket/sizing kontrolleri, borsada stop doğrulama ve hata durumunda pozisyon koruma yaşam döngüsü korunur.
Yeni Radar izni bu kontrolleri atlamaz. Mevcut testnet/live gate dosyaları değiştirilmedi.
Gerçek Demo anahtarlarıyla işlem yaşam döngüsü denenmedi. Üretilmiş bir gerçek testnet kanıtı veya yeni canlı onay yoktur. Canlı mod etkinleştirilmedi.
Bulut dosyaları
Bileşen
Bulgular / işlem
Dockerfile
/data, port ortam değişkeni, /health healthcheck ve SIGTERM mevcut; değişmedi
docker-entrypoint.sh
Kalıcı dizin sahipliği sonrası gosu ile yetki düşürme mevcut; değişmedi
railway.json
Dockerfile, /health ve numReplicas=1 mevcut; değişmedi
render.yaml
/data kalıcı disk ve MODE=dry korunur; Radar işlem özelliği açıkça false eklenir
CI
Yeni kabul test dosyası mevcut test matrisine eklendi; güvenlik ve Docker işleri kaldırılmadı
render.yaml içindeki eski imperion-finance-v16 servis adı bilinçli korunmuştur. Bu bir Render kaynak kimliği olabilir; salt ürün sürümü için değiştirmek yeni servis oluşturmaya yol açabileceğinden otomatik isim değişikliği yapılmadı.
Dağıtım sırasında gerekenler
Güncelleme dosyalarını birlikte uygulayın; CI bitmeden dağıtmayın.
Başlangıçta MODE=dry ve RADAR_TRADING_ENABLED=false bırakın.
Railway kullanılıyorsa servise /data mount yoluyla bir Volume bağlayın. railway.json tek başına Volume oluşturmaz. Render Blueprint içindeki disk ayarını kontrol edin. Birden çok bot süreci/replica aynı SQLite dosyasına yazmamalı.
ADMIN_TOKEN ve TradingView secret değerlerini sağlayıcının gizli değişken bölümünde belirleyin; örnek dosyaya gerçek anahtar yazmayın.
/health hazır olmayı bildirir; piyasanın güncel olduğunu veya işlem yapılabildiğini tek başına kanıtlamaz. /status içindeki canlı kapı, stream ve sembol sağlığı alanlarını da inceleyin.
Servisi yeniden başlatıp veritabanının, trade kayıtlarının ve risk referanslarının kaldığını doğrulayın. Ardından kontrollü dry çalışmada Radar listesi ve ret nedenlerini inceleyin.
Gerçek Testnet doğrulaması ayrı aşamadır; canlı onay komutunu bu paket otomatik çalıştırmaz.
Kalıcı disk/Volume hesabı açılmadı, ücretli kaynak oluşturulmadı ve herhangi bir sunucuya dağıtım yapılmadı. Bu ortamda Docker çalıştırıcısı bulunmadığı için container build bu çalışmada denenemedi. Python testleri, release kontrolü ve selftest sonuçları güncelleme kılavuzundadır.
Resmî kaynaklar (12 Kasım 2026 tarihinde kontrol edildi):
https://render.com/docs/disks
https://render.com/docs/health-checks
https://docs.railway.com/volumes
