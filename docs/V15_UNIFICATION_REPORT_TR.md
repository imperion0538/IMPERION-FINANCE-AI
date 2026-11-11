# IMPERION FINANCE V15.0.0 — Birleştirme ve Teknik Teslim Raporu

## Kaynak seçimi
- V14.2.1 HARDENED, runtime/execution/risk/data/learning/research katmanları için temel alındı.
- V14.4.0 ADVANCED PA'nın yeni Price Action modülleri ve playbook'ları entegre edildi.
- V14.2.1'in `TradeStore.flow_profile()` (CVD, delta ratio, price-level volume, POC) ve SMT modülü korundu.

## Entegre edilen Advanced PA bileşenleri
- `inducement.py`: internal liquidity/IDM tespiti.
- `structure.py`: dealing range ve Fibonacci OTE bilgisi.
- `zones.py`: gelişmiş zone scoring ve mitigation block üretimi.
- `sessions.py`: Asia/London/New York UTC session ranges.
- `cisd.py`: displacement/CISD doğrulaması.
- `playbooks.py`: Inducement Sweep, OTE Pullback, Mitigation Retest, Session CISD.

## Birleştirme sırasında düzeltilenler
1. `zones.describe()` MITIGATION zone tipini güvenli biçimde yazdırır; önceki tuple lookup KeyError giderildi.
2. Session range'leri UTC tarihine göre gruplanır; farklı günlerin aynı saat aralıkları birleştirilmez.
3. IDM sweep araması ana sweep mumundan önceki mumlarla sınırlıdır; aynı mumda IDM ve ana sweep oluşmuş gibi değerlendirilmez.
4. SMT export'u Advanced PA klasörüne geri eklendi.
5. Sürüm metadata'sı `15.0.0` SemVer olacak şekilde eşitlendi.
6. Öğrenme modeli `CORE_VERSION=agents-1-unified-pa` ile yeni analiz mantığından önceki shadow verilerini ayrı tutar.
7. Test beklentileri yeni varsayılan risk profiline göre güncellendi.

## Varsayılan risk
- Risk / trade: %0,25
- Eşzamanlı pozisyon: 1
- Açık toplam risk: %0,50
- Günlük yeni risk: %1,00
- Günlük zarar durdurma: %1,50
- Maksimum drawdown: %5,00
- Kaldıraç: 2x, azami 2x

Bunlar kod varsayılanlarıdır; kullanıcı konfigürasyonu tarafından değiştirilebilir. Risk limiti kâr veya kayıp garantisi değildir.

## Dağıtım içeriği
- GitHub Actions CI: Python 3.11/3.12/3.13 test matrisleri, dependency audit, secret scan, selftest ve Docker build.
- Release workflow: sürüm etiketi, CI, Testnet kanıtı, `production` environment onayı, GitHub Release ve GHCR container image.
- Dockerfile: non-root runtime, `/data` kalıcı depolama, healthcheck `/health`.
- Railway: `railway.json` Dockerfile build konfigürasyonu.
- Render: `render.yaml` Blueprint örneği; persistent disk `/data`.

## Bu teslimde çalıştırılan kontroller
- Python compileall: PASS
- Release metadata: PASS
- CI workflow / release workflow / testnet workflow YAML parse: PASS
- Render YAML ve Railway JSON parse: PASS
- `python -m imperion selftest`: PASS (9 senaryo)
- Pytest CI shard'ları: 70 + 65 + 27 + 49 = 211 PASS

Docker CLI bu çalışma ortamında kurulu olmadığından yerel image build çalıştırılamadı. GitHub Actions Docker job'ı push sonrası gerçek CI ortamında build ve healthcheck yapacak. Binance Demo/Testnet veya gerçek hesapta bağlantı/emir testi bu teslim ortamında yapılmadı.

## Canlıya geçiş şartları
- İlk deploy `MODE=dry` olmalı.
- `/data` persistent volume bağlanmalı ve tek aktif replica kullanılmalı.
- Testnet API key ve HMAC evidence secret GitHub/cloud secret manager'da tanımlanmalı.
- Testnet emir yaşam döngüsü, one-way/single-asset modu ve gerçek liquidationPrice doğrulanmalı.
- İmzalı kanıt içe aktarılmalı ve operatör onayı verilmelidir.
- `MODE=live` ancak tüm kapılar geçildikten sonra, ayrı ve bilinçli operatör kararıyla açılmalıdır.
