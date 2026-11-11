# V17.0.0 Birleştirme Raporu

## Kaynak kararları
- Ana kaynak: V17.0.0.
- V14.3.0'dan: üretim runbook'u ve release bundle doğrulayıcısı.
- V15.2.0'dan: bağımsız SMT ajanı.
- V14/V15/V16 ortak execution, risk, guardian ve Testnet kapısı V16 temelinde korunmuştur.

## Güvenlik ilkeleri
1. Varsayılan `MODE=dry`.
2. Canlı emirler için mevcut kod kapıları korunur.
3. Stop emri doğrulanmadan pozisyonun güvenli olduğu varsayılmaz.
4. Risk limitleri ve kill switch fail-closed davranmalıdır.
5. Testnet kanıtı güncel, imzalı ve Live onayına bağlı olmalıdır.
6. API anahtarında para çekme izni kesinlikle açılmamalıdır.

## Bilinçli sınırlar
Bu rapor kaynak birleştirme ve statik doğrulamayı açıklar. Gerçek Binance Testnet, bulut deploy, dependency audit ve kârlılık testi yapıldığı anlamına gelmez.
