# Bulut Dağıtım Rehberi (Türkçe)

## GitHub
- Yeni bir repository oluşturun ve proje kök dosyalarını yükleyin.
- `main` branch'e push edin; Actions → CI workflow'unun tamamlanmasını bekleyin.
- GitHub Actions secrets'a sadece gerekli değerleri ekleyin. İlk CI/dry deploy için Binance anahtarı gerekmez.
- `v17.0.0` etiketi release akışını tetikler; workflow testnet kanıtı ve `production` environment onayı ister.

## Railway
- New Project → Deploy from GitHub Repo → bu repository.
- Dockerfile otomatik algılanır (`railway.json`).
- Volume ekleyin, mount path `/data` olsun.
- Variables bölümünde `MODE=dry`, `IMPERION_STORAGE_DIR=/data`, `PORT` (Railway sağlar) ve gerekli güvenlik tokenlarını tanımlayın.
- Healthcheck `/health`; tek replica kullanın.

## Render
- New → Blueprint ile `render.yaml` dosyasını kullanın veya Docker Web Service oluşturun.
- Persistent Disk mount path `/data` olmalı.
- İlk deploy'da `MODE=dry` bırakın. Gizli değişkenleri dashboard'da tanımlayın; YAML'a secret yazmayın.

## Güvenlik
- `MODE=live` ile ilk deploy yapmayın.
- Binance key'de withdrawal izni kapalı olsun; IP allowlist kullanın.
- `TESTNET_EVIDENCE_SECRET` en az 32 karakter rastgele değer olmalı ve kanıtı üreten ortamla sunucuda aynı olmalı.
- `ADMIN_TOKEN` ve webhook secret'ları en az 16 karakter rastgele değer olmalı.
- Persistent disk olmadan SQLite ve testnet kanıtı restart sonrası kaybolabilir.
- Bulut sağlayıcıda tek replica/tek worker kullanın; aynı veritabanını birden fazla aktif bot süreci paylaşmasın.
