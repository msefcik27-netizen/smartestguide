"""Texty tiskovin (roll-up, QR plakát, letáky) v jazycích hostů.

Klíče: n = název jazyka v něm samém, h = nadpis (<br> = zalomení), sub = podtitul
({a} = jméno Alex), a = jméno Alex v daném písmu, f = pět výhod, scan = krátká výzva,
long = dlouhá výzva pod QR kódem, noapp = bez instalace, langs = "100+ jazyků",
rtl = písmo zprava doleva.
"""

_T = {
    "en": dict(n="English", h="Your personal<br>AI concierge",
               sub="Let {a} answer all your questions — instantly, in your language, 24/7.",
               f=["Breakfast times & restaurant info", "Local tips & hidden gems", "Weather & transport", "Hotel services & WiFi", "Available 24/7"],
               scan="Scan me", long="Scan for your personal AI concierge", noapp="No app needed", langs="100+ languages"),
    "cs": dict(n="Česky", h="Váš osobní<br>AI concierge",
               sub="Nechte {a} odpovědět na všechny vaše otázky — okamžitě, ve vašem jazyce, 24/7.",
               f=["Snídaně a restaurace", "Tipy na výlety a skrytá místa", "Počasí a doprava", "Služby hotelu a WiFi", "K dispozici 24/7"],
               scan="Naskenujte mě", long="Naskenujte a získejte osobního AI concierge", noapp="Bez instalace aplikace", langs="100+ jazyků"),
    "sk": dict(n="Slovenčina", h="Váš osobný<br>AI concierge",
               sub="Nechajte {a} odpovedať na všetky vaše otázky — okamžite, vo vašom jazyku, 24/7.",
               f=["Raňajky a reštaurácia", "Tipy na výlety a skryté miesta", "Počasie a doprava", "Služby hotela a WiFi", "K dispozícii 24/7"],
               scan="Naskenujte ma", long="Naskenujte a získajte osobného AI concierge", noapp="Bez inštalácie aplikácie", langs="100+ jazykov"),
    "de": dict(n="Deutsch", h="Ihr persönlicher<br>AI-Concierge",
               sub="Lassen Sie {a} alle Ihre Fragen beantworten — sofort, in Ihrer Sprache, 24/7.",
               f=["Frühstück & Restaurant", "Ausflugstipps & versteckte Orte", "Wetter & Transport", "Hotelservices & WLAN", "Rund um die Uhr verfügbar"],
               scan="Scannen Sie mich", long="Scannen und Ihren persönlichen AI-Concierge starten", noapp="Keine App erforderlich", langs="100+ Sprachen"),
    "fr": dict(n="Français", h="Votre concierge<br>IA personnel",
               sub="Laissez {a} répondre à toutes vos questions — instantanément, dans votre langue, 24/7.",
               f=["Petit-déjeuner & restaurant", "Conseils locaux & lieux cachés", "Météo & transport", "Services hôtel & WiFi", "Disponible 24h/24"],
               scan="Scannez-moi", long="Scannez pour accéder à votre concierge IA personnel", noapp="Sans installation d'app", langs="100+ langues"),
    "it": dict(n="Italiano", h="Il vostro concierge<br>IA personale",
               sub="Lasciate che {a} risponda a tutte le vostre domande — immediatamente, nella vostra lingua, 24/7.",
               f=["Colazione & ristorante", "Consigli locali & luoghi nascosti", "Meteo & trasporti", "Servizi hotel & WiFi", "Disponibile 24/7"],
               scan="Scansionami", long="Scansionate per il vostro concierge IA personale", noapp="Nessuna app richiesta", langs="100+ lingue"),
    "es": dict(n="Español", h="Su concierge<br>IA personal",
               sub="Deje que {a} responda todas sus preguntas — al instante, en su idioma, 24/7.",
               f=["Desayuno & restaurante", "Consejos locales & lugares ocultos", "Tiempo & transporte", "Servicios hotel & WiFi", "Disponible 24/7"],
               scan="Escanéame", long="Escanee para acceder a su concierge IA personal", noapp="Sin instalación de app", langs="100+ idiomas"),
    "pl": dict(n="Polski", h="Twój osobisty<br>concierge AI", a="Alexowi",
               sub="Pozwól {a} odpowiedzieć na wszystkie Twoje pytania — natychmiast, w Twoim języku, 24/7.",
               f=["Śniadanie & restauracja", "Lokalne wskazówki & ukryte miejsca", "Pogoda & transport", "Usługi hotelowe & WiFi", "Dostępny 24/7"],
               scan="Zeskanuj mnie", long="Zeskanuj i skorzystaj z osobistego concierge AI", noapp="Bez instalacji aplikacji", langs="100+ języków"),
    "hu": dict(n="Magyar", h="Az Ön személyes<br>AI concierge-e",
               sub="Hagyja, hogy {a} azonnal válaszoljon minden kérdésére — az Ön nyelvén, 24/7.",
               f=["Reggeli és étterem", "Helyi tippek és rejtett helyek", "Időjárás és közlekedés", "Szállodai szolgáltatások és WiFi", "Elérhető 24/7"],
               scan="Szkenneljen be", long="Olvassa be, és máris elérhető személyes AI concierge-e", noapp="Nincs szükség alkalmazásra", langs="100+ nyelv"),
    "nl": dict(n="Nederlands", h="Uw persoonlijke<br>AI-conciërge",
               sub="Laat {a} al uw vragen beantwoorden — direct, in uw taal, 24/7.",
               f=["Ontbijt & restaurant", "Lokale tips & verborgen plekjes", "Weer & vervoer", "Hotelservices & wifi", "24/7 beschikbaar"],
               scan="Scan mij", long="Scan voor uw persoonlijke AI-conciërge", noapp="Geen app nodig", langs="100+ talen"),
    "pt": dict(n="Português", h="O seu concierge<br>de IA pessoal",
               sub="Deixe o {a} responder a todas as suas perguntas — de imediato, no seu idioma, 24/7.",
               f=["Pequeno-almoço e restaurante", "Dicas locais e lugares escondidos", "Tempo e transportes", "Serviços do hotel e WiFi", "Disponível 24/7"],
               scan="Digitalize-me", long="Digitalize para aceder ao seu concierge de IA pessoal", noapp="Sem instalar aplicação", langs="100+ idiomas"),
    "ru": dict(n="Русский", h="Ваш персональный<br>AI-консьерж", a="Алекс",
               sub="{a} ответит на все ваши вопросы — мгновенно, на вашем языке, 24/7.",
               f=["Завтрак и ресторан", "Местные советы и скрытые места", "Погода и транспорт", "Услуги отеля и WiFi", "Доступен 24/7"],
               scan="Отсканируйте меня", long="Отсканируйте — и ваш персональный AI-консьерж на связи", noapp="Без установки приложения", langs="100+ языков"),
    "uk": dict(n="Українська", h="Ваш персональний<br>AI-консьєрж", a="Алекс",
               sub="{a} відповість на всі ваші запитання — миттєво, вашою мовою, 24/7.",
               f=["Сніданок і ресторан", "Місцеві поради та приховані місця", "Погода і транспорт", "Послуги готелю та WiFi", "Доступний 24/7"],
               scan="Відскануйте мене", long="Відскануйте — і ваш персональний AI-консьєрж на зв’язку", noapp="Без встановлення додатку", langs="100+ мов"),
    "ja": dict(n="日本語", h="あなた専用の<br>AIコンシェルジュ",
               sub="{a}がすべてのご質問にお答えします — すぐに、あなたの言語で、24時間365日。",
               f=["朝食・レストラン", "地元のおすすめ・穴場スポット", "天気・交通", "ホテルのサービス・WiFi", "24時間いつでも利用可能"],
               scan="スキャンしてください", long="スキャンしてAIコンシェルジュをご利用ください", noapp="アプリ不要", langs="100以上の言語"),
    "zh": dict(n="中文", h="您的专属<br>AI礼宾",
               sub="让{a}即时用您的语言解答所有问题，全天候服务。",
               f=["早餐与餐厅", "本地推荐与小众景点", "天气与交通", "酒店服务与WiFi", "全天候 24/7"],
               scan="请扫码", long="扫码使用您的专属AI礼宾", noapp="无需下载应用", langs="100多种语言"),
    "ko": dict(n="한국어", h="나만의<br>AI 컨시어지",
               sub="{a}가 모든 질문에 즉시, 고객님의 언어로, 24시간 답해 드립니다.",
               f=["조식 및 레스토랑", "현지 추천 및 숨은 명소", "날씨 및 교통", "호텔 서비스 및 WiFi", "24시간 이용 가능"],
               scan="스캔하세요", long="스캔하여 나만의 AI 컨시어지를 만나보세요", noapp="앱 설치 불필요", langs="100개 이상 언어"),
    "el": dict(n="Ελληνικά", h="Ο προσωπικός σας<br>AI concierge",
               sub="Αφήστε τον {a} να απαντήσει σε όλες τις ερωτήσεις σας — άμεσα, στη γλώσσα σας, 24/7.",
               f=["Πρωινό & εστιατόριο", "Τοπικές προτάσεις & κρυφά σημεία", "Καιρός & μετακινήσεις", "Υπηρεσίες ξενοδοχείου & WiFi", "Διαθέσιμος 24/7"],
               scan="Σκανάρετέ με", long="Σκανάρετε για τον προσωπικό σας AI concierge", noapp="Χωρίς εγκατάσταση εφαρμογής", langs="100+ γλώσσες"),
    "sv": dict(n="Svenska", h="Din personliga<br>AI-concierge",
               sub="Låt {a} svara på alla dina frågor — direkt, på ditt språk, dygnet runt.",
               f=["Frukost & restaurang", "Lokala tips & dolda pärlor", "Väder & transport", "Hotellservice & WiFi", "Tillgänglig dygnet runt"],
               scan="Skanna mig", long="Skanna för din personliga AI-concierge", noapp="Ingen app behövs", langs="100+ språk"),
    "da": dict(n="Dansk", h="Din personlige<br>AI-concierge",
               sub="Lad {a} svare på alle dine spørgsmål — med det samme, på dit sprog, døgnet rundt.",
               f=["Morgenmad & restaurant", "Lokale tips & skjulte perler", "Vejr & transport", "Hotellets services & WiFi", "Tilgængelig døgnet rundt"],
               scan="Scan mig", long="Scan og få din personlige AI-concierge", noapp="Ingen app nødvendig", langs="100+ sprog"),
    "no": dict(n="Norsk", h="Din personlige<br>AI-concierge",
               sub="La {a} svare på alle spørsmålene dine — umiddelbart, på ditt språk, døgnet rundt.",
               f=["Frokost & restaurant", "Lokale tips & skjulte perler", "Vær & transport", "Hotellets tjenester & WiFi", "Tilgjengelig døgnet rundt"],
               scan="Skann meg", long="Skann for din personlige AI-concierge", noapp="Ingen app nødvendig", langs="100+ språk"),
    "fi": dict(n="Suomi", h="Henkilökohtainen<br>AI-conciergesi",
               sub="{a} vastaa kaikkiin kysymyksiisi — heti, omalla kielelläsi, vuorokauden ympäri.",
               f=["Aamiainen ja ravintola", "Paikalliset vinkit ja piilotetut helmet", "Sää ja liikenne", "Hotellin palvelut ja WiFi", "Käytettävissä 24/7"],
               scan="Skannaa minut", long="Skannaa ja ota AI-concierge käyttöösi", noapp="Sovellusta ei tarvita", langs="100+ kieltä"),
    "ro": dict(n="Română", h="Concierge-ul tău<br>AI personal",
               sub="Lasă-l pe {a} să răspundă la toate întrebările tale — instant, în limba ta, 24/7.",
               f=["Mic dejun și restaurant", "Sfaturi locale și locuri ascunse", "Vreme și transport", "Serviciile hotelului și WiFi", "Disponibil 24/7"],
               scan="Scanează-mă", long="Scanează pentru concierge-ul tău AI personal", noapp="Fără instalarea unei aplicații", langs="100+ limbi"),
    "bg": dict(n="Български", h="Вашият личен<br>AI консиерж", a="Алекс",
               sub="Нека {a} отговори на всичките ви въпроси — веднага, на вашия език, 24/7.",
               f=["Закуска и ресторант", "Местни съвети и скрити места", "Време и транспорт", "Хотелски услуги и WiFi", "На разположение 24/7"],
               scan="Сканирайте ме", long="Сканирайте за вашия личен AI консиерж", noapp="Без инсталиране на приложение", langs="100+ езика"),
    "hr": dict(n="Hrvatski", h="Vaš osobni<br>AI concierge",
               sub="Neka {a} odgovori na sva vaša pitanja — odmah, na vašem jeziku, 24/7.",
               f=["Doručak i restoran", "Lokalni savjeti i skrivena mjesta", "Vrijeme i prijevoz", "Hotelske usluge i WiFi", "Dostupan 24/7"],
               scan="Skenirajte me", long="Skenirajte za svog osobnog AI conciergea", noapp="Bez instalacije aplikacije", langs="100+ jezika"),
    "sl": dict(n="Slovenščina", h="Vaš osebni<br>AI concierge",
               sub="Naj {a} odgovori na vsa vaša vprašanja — takoj, v vašem jeziku, 24/7.",
               f=["Zajtrk in restavracija", "Lokalni nasveti in skriti kotički", "Vreme in prevoz", "Hotelske storitve in WiFi", "Na voljo 24/7"],
               scan="Skenirajte me", long="Skenirajte za svojega osebnega AI conciergea", noapp="Brez nameščanja aplikacije", langs="100+ jezikov"),
    "sr": dict(n="Srpski", h="Vaš lični<br>AI concierge",
               sub="Neka {a} odgovori na sva vaša pitanja — odmah, na vašem jeziku, 24/7.",
               f=["Doručak i restoran", "Lokalni saveti i skrivena mesta", "Vreme i prevoz", "Hotelske usluge i WiFi", "Dostupan 24/7"],
               scan="Skenirajte me", long="Skenirajte za svog ličnog AI conciergea", noapp="Bez instalacije aplikacije", langs="100+ jezika"),
    "tr": dict(n="Türkçe", h="Kişisel<br>AI concierge’iniz",
               sub="{a} tüm sorularınızı anında, kendi dilinizde, 7/24 yanıtlasın.",
               f=["Kahvaltı ve restoran", "Yerel öneriler ve gizli köşeler", "Hava durumu ve ulaşım", "Otel hizmetleri ve WiFi", "7/24 hizmetinizde"],
               scan="Beni tarayın", long="Kişisel AI concierge’iniz için tarayın", noapp="Uygulama gerekmez", langs="100+ dil"),
    "et": dict(n="Eesti", h="Teie isiklik<br>AI-concierge",
               sub="{a} vastab kõigile teie küsimustele — kohe, teie keeles, ööpäev läbi.",
               f=["Hommikusöök ja restoran", "Kohalikud soovitused ja peidetud paigad", "Ilm ja transport", "Hotelli teenused ja WiFi", "Saadaval ööpäev läbi"],
               scan="Skanni mind", long="Skannige ja kasutage oma isiklikku AI-concierge’i", noapp="Rakendust pole vaja", langs="100+ keelt"),
    "lv": dict(n="Latviešu", h="Jūsu personīgais<br>AI konsjeržs", a="Alekss",
               sub="{a} atbildēs uz visiem jūsu jautājumiem — uzreiz, jūsu valodā, visu diennakti.",
               f=["Brokastis un restorāns", "Vietējie ieteikumi un apslēptas vietas", "Laikapstākļi un transports", "Viesnīcas pakalpojumi un WiFi", "Pieejams 24/7"],
               scan="Noskenē mani", long="Noskenējiet un izmantojiet savu AI konsjeržu", noapp="Nav jāinstalē lietotne", langs="100+ valodas"),
    "lt": dict(n="Lietuvių", h="Jūsų asmeninis<br>AI konsjeržas",
               sub="{a} atsakys į visus jūsų klausimus — iš karto, jūsų kalba, visą parą.",
               f=["Pusryčiai ir restoranas", "Vietiniai patarimai ir slaptos vietos", "Orai ir transportas", "Viešbučio paslaugos ir WiFi", "Pasiekiamas 24/7"],
               scan="Nuskenuok mane", long="Nuskenuokite ir naudokitės savo AI konsjeržu", noapp="Nereikia programėlės", langs="100+ kalbų"),
    "ar": dict(n="العربية", rtl=True, h="مساعدك الشخصي<br>بالذكاء الاصطناعي",
               sub="دع {a} يجيب عن جميع أسئلتك — فورًا، بلغتك، على مدار الساعة.",
               f=["الإفطار والمطعم", "نصائح محلية وأماكن مميزة", "الطقس والمواصلات", "خدمات الفندق والواي فاي", "متاح على مدار الساعة"],
               scan="امسح الرمز", long="امسح الرمز للوصول إلى مساعدك الشخصي", noapp="بدون تثبيت تطبيق", langs="أكثر من 100 لغة"),
    "he": dict(n="עברית", rtl=True, h="הקונסיירז׳ האישי שלכם<br>מבוסס AI",
               sub="{a} יענה על כל השאלות שלכם — מיד, בשפה שלכם, 24/7.",
               f=["ארוחת בוקר ומסעדה", "המלצות מקומיות ופינות חבויות", "מזג אוויר ותחבורה", "שירותי המלון ו-WiFi", "זמין 24/7"],
               scan="סרקו אותי", long="סרקו לקבלת הקונסיירז׳ האישי שלכם", noapp="ללא התקנת אפליקציה", langs="יותר מ-100 שפות"),
}

# pořadí ve výběru jazyka: nejčastější napřed
ORDER = ["en", "cs", "sk", "de", "fr", "it", "es", "pl", "hu", "nl", "pt", "uk", "ru",
         "sv", "da", "no", "fi", "ro", "bg", "hr", "sl", "sr", "el", "tr", "et", "lv", "lt",
         "ja", "zh", "ko", "ar", "he"]
_ALIAS = {"cz": "cs", "se": "sv", "dk": "da", "gr": "el", "nb": "no", "ua": "uk", "jp": "ja", "cn": "zh", "kr": "ko"}


def norm(lang: str) -> str:
    """Kód jazyka, pro který máme texty; regionální varianty na základ, neznámé na en."""
    code = (lang or "").strip().lower().replace("_", "-")
    code = _ALIAS.get(code, code)
    if code not in _T:
        code = code.split("-")[0]
        code = _ALIAS.get(code, code)
    return code if code in _T else "en"


def get(lang: str) -> dict:
    code = norm(lang)
    t = dict(_T[code])
    t.setdefault("a", "Alex")
    t.setdefault("rtl", False)
    t["code"] = code
    t["tag"] = t["h"].replace("<br>", " ")
    return t


def options() -> list:
    """[(kód, název jazyka)] pro výběr v rozcestníku tiskovin."""
    return [(c, _T[c]["n"]) for c in ORDER]


assert set(ORDER) == set(_T), "ORDER a _T se rozcházejí"
