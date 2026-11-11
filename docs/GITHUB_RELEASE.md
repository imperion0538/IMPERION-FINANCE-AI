# GitHub yayın hattı kurulumu (v14.2.1)

## İş akışları

| Dosya | Ne zaman | Ne yapar |
|---|---|---|
| `.github/workflows/ci.yml` | her push / PR, ayrıca release tarafından çağrılır | Python 3.11 / 3.12 / 3.13 tam test (dört bağımsız test grubu) (sızan iş parçacığı = hata), selftest, `pip check`, sürüm tutarlılığı; `pip-audit` + gitleaks; Docker derleme, imaj içinde selftest, root olmayan kullanıcı, HEALTHCHECK, `/health` ve SIGTERM ile 15 sn içinde 0 çıkış kodu |
| `.github/workflows/testnet.yml` | elle (`workflow_dispatch`) veya release tarafından çağrılır | Binance Futures Demo: One-way mod, emir yaşam döngüsü, gerçek `liquidationPrice` kontrolü → HMAC imzalı kanıt → ayrı adımda `verify-evidence` → `testnet-evidence` artifact'ı (30 gün) + özet (`evidence_sha256` çıktısı) |
| `.github/workflows/release.yml` | `v*` etiketi (ör. `v14.2.1`) veya elle | etiket = `VERSION` → tam CI → `testnet.yml` → kanıt indirilir ve özetiyle yeniden doğrulanır → `production` ortamında elle onay → zip + sha256 + imzalı kanıt ile GitHub Release |

Herhangi bir kapı başarısızsa yayın oluşmaz.

## Bir kez yapılacak ayarlar (Settings)

1. **Environments → `testnet`**: secrets `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET` (Binance Futures **Demo** anahtarları).
   **Repository secret** `TESTNET_EVIDENCE_SECRET` (en az 32 karakter; testnet ve publish işleri ikisi de okur). Aynı değeri
   canlı sunucunun ortamına da yazın: sunucu kanıtı bu anahtarla doğrular.
2. **Variables** (repository): `TESTNET_ORDER_QTY` (ör. BTCUSDT için `0.002`; notional borsa minimumunun üstünde, `TESTNET_MAX_NOTIONAL_USDT` altında olmalı),
   isteğe bağlı `TESTNET_SYMBOL` (varsayılan BTCUSDT), `TESTNET_MAX_NOTIONAL_USDT` (varsayılan 150).
3. **Environments → `production`**: *Required reviewers* ekleyin. Yayın bu onay olmadan yapılmaz.
4. Binance ABD'deki GitHub sunucularını reddedebilir (HTTP 451). O durumda Avrupa'da bir *self-hosted runner* kurup
   `TESTNET_RUNNER` değişkenine etiketini yazın.
5. Organizasyona ait depoda gitleaks için `GITLEAKS_LICENSE` secret'ı gerekir (kişisel depoda gerekmez).

## İlk GitHub yüklemesi (boş depo için)

Depo henüz boşsa, ZIP'i açtıktan sonra proje kökünde aşağıdakileri çalıştırın. `main` dalına ilk kaynak commit'i gönderir; sonraki sürümler ayrı dallarda/etiketlerde hazırlanabilir.

```bash
git init -b main
git add .
git commit -m "Initial import: IMPERION FINANCE V14.2.1 HARDENED"
git remote add origin https://github.com/imperion0538/IMPERION-FINANCE-AI.git
git push -u origin main
```

## Yayın

```bash
git tag v14.2.1 && git push origin v14.2.1
```

## Önemli ayrım

CI'daki Testnet kanıtı **yayının** kapısıdır. Sunucunuzdaki **canlı işlem kapısı** sunucunun kendi veritabanında tutulur.
İki yoldan biri yeterli:

* **GitHub kanıtı:** `testnet` iş akışının (ya da yayının) `testnet-evidence` artifact'ını indirin, sunucuda
  `python -m imperion verify-evidence testnet-evidence.json` → `python -m imperion import-evidence testnet-evidence.json`.
* **Sunucuda doğrulama:** `MODE=testnet` ile `python -m imperion testnet-check` (emir testi açık).

Ardından `python -m imperion approve-live --operator ADINIZ --confirm I_APPROVE_LIVE_TRADING`. Kanıt imzası her
değerlendirmede yeniden kontrol edilir; sunucudaki `TESTNET_EVIDENCE_SECRET` kanıtı imzalayan anahtardan farklıysa
`MODE=live` emir göndermez.

**İmzanın sınırı:** paylaşılan anahtarlı HMAC, kanıtın anahtarı tutan doğrulayıcı tarafından üretildiğini ve sonradan
değiştirilmediğini kanıtlar; anahtarı zaten ele geçirmiş birine karşı koruma sağlamaz. Anahtarı yalnızca GitHub
secret'larında ve sunucunun ortamında tutun.
