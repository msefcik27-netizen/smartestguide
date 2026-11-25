# -*- coding: utf-8 -*-
"""Testy pojistek nad odpovědí Alex: ženský rod a cizí písmo.
Každý nález testera má tady svou větu — když se chyba vrátí, test spadne."""
import pytest

import reply_guard as rg


# ── Ženský rod: věty, které se MAJÍ opravit ──────────────────────────────────
@pytest.mark.parametrize("vstup, cekam", [
    # nález testera 24. 11. 2026 (WhatsApp 19:12)
    ("Abych ti to mohl ověřit, potřebuji číslo pokoje.",
     "Abych ti to mohla ověřit, potřebuji číslo pokoje."),
    ("Podíval jsem se do nabídky hotelu.", "Podívala jsem se do nabídky hotelu."),
    ("To jsem bohužel nenašel.", "To jsem bohužel nenašla."),
    ("Rád bych ti pomohl.", "Ráda bych ti pomohla."),
    ("Byl bych rád, kdyby to vyšlo.", "Byla bych ráda, kdyby to vyšlo."),
    ("Kdybych to věděl, řekla bych ti to.", "Kdybych to věděla, řekla bych ti to."),
    ("Nejsem si jistý, zda je bazén otevřený.", "Nejsem si jistá, zda je bazén otevřený."),
    ("Jsem si jist, že ano.", "Jsem si jistá, že ano."),
    ("Jsem tvůj virtuální asistent.", "Jsem tvoje virtuální asistentka."),
    ("Jsem Alex, váš hotelový průvodce.", "Jsem Alex, váš hotelový průvodce."),  # čárka = jiný úsek
    ("Jsem váš hotelový průvodce.", "Jsem vaše hotelová průvodkyně."),
    ("Mrzí mě, že jsem ti nepomohl.", "Mrzí mě, že jsem ti nepomohla."),
    ("Už jsem ti to jednou poslal.", "Už jsem ti to jednou poslala."),
    ("Zeptal jsem se recepce.", "Zeptala jsem se recepce."),
    ("Tohle bych ti určitě doporučil.", "Tohle bych ti určitě doporučila."),
    ("Rád ti pomůžu s čímkoli.", "Ráda ti pomůžu s čímkoli."),
    ("rád vám to zjistím", "ráda vám to zjistím"),
    ("Jsem připraven pomoci.", "Jsem připravena pomoci."),
    ("Přišel jsem na to.", "Přišla jsem na to."),
    ("Slyšel jsem, že je tam hezky.", "Slyšela jsem, že je tam hezky."),
    # slovensky
    ("Našiel som to v ponuke.", "Našla som to v ponuke."),
    ("Rád by som pomohol.", "Rada by som pomohla."),
    ("Bol som si istý.", "Bola som si istá."),
])
def test_zensky_rod_opravi(vstup, cekam):
    assert rg.feminine_fix(vstup) == cekam


# ── Ženský rod: věty, které se měnit NESMÍ ───────────────────────────────────
@pytest.mark.parametrize("veta", [
    "Bohužel jsem to nenašla.",                       # „bohužel“ není sloveso
    "Tenhle hotel jsem už doporučila.",               # podstatné jméno před „jsem“
    "Jsem ráda, že host přišel včas.",                # jiný podmět za čárkou
    "Zjistila jsem, že bazén otevřel v osm.",
    "Personál vám rád pomůže.",                       # třetí osoba
    "Recepční vám rád poradí.",
    "Kdybyste chtěl, zařídím to.",                    # oslovení hosta
    "Byl jste už na snídani?",
    "Ráda bych ti doporučila hotel Karel.",           # vlastní jméno
    "Podívala jsem se a detail najdeš níže.",
    "Jsem tu pro tebe celý den.",
    "I would be glad to help, Michael.",              # cizí jazyk
    "Jsem ráda že ti kolega pomohl.",                 # „že“ ukončí úsek i bez čárky
    "Wellness areál jsem ti už popsala.",
])
def test_zensky_rod_nesaha(veta):
    assert rg.feminine_fix(veta) == veta


def test_zensky_rod_prazdny_vstup():
    assert rg.feminine_fix("") == ""
    assert rg.feminine_fix(None) is None


# ── Cizí písmo ───────────────────────────────────────────────────────────────
def test_azbuka_v_ceske_odpovedi():
    # nález testera 24. 11. 2026 (WhatsApp 19:14)
    vstup = "Recepce: +420 224 811 234\nПерсонал ti může také poslat ručníky."
    vysl = rg.fix_foreign_script(vstup, "cs", "jaké je číslo na recepci")
    assert "Персонал" not in vysl
    assert "Personál ti může také poslat ručníky." in vysl


def test_azbuka_neznama_slova_se_prepisou():
    vysl = rg.fix_foreign_script("Doporučuji бассейн v přízemí.", "cs", "kde je bazén")
    assert vysl == "Doporučuji bassejn v přízemí."


def test_azbuka_zatoulane_pismeno():
    # „с“ a „е“ v azbuce uvnitř latinkou psaného slova
    vysl = rg.fix_foreign_script("reсepсe je otevřená", "cs", "")
    assert vysl == "recepce je otevřená"


@pytest.mark.parametrize("lang, zprava", [
    ("ru", "где бассейн"),      # ruská odpověď
    ("uk", "де басейн"),
    ("cs", "co znamená слово"),  # host sám psal azbukou
])
def test_azbuka_se_nechava(lang, zprava):
    text = "Персонал поможет."
    assert rg.fix_foreign_script(text, lang, zprava) == text


def test_azbuka_souvisly_kus_ve_slove():
    # noční zkouška 24. 11. 2026: dřív „ictopií“ a „mectní“
    assert rg.fix_foreign_script("seznámení se s историí", "cs", "") == "seznámení se s istorií"
    assert rg.fix_foreign_script("местní tipy", "cs", "") == "mestní tipy"


def test_cinske_znaky_v_ceske_vete():
    vysl = rg.drop_stray_cjk("přímo recepce —联系 je můžete:", "cs", "kde je recepce")
    assert vysl == "přímo recepce — je můžete:"
    assert rg.drop_stray_cjk("e-mail a check-in", "cs", "") == "e-mail a check-in"


@pytest.mark.parametrize("lang,zprava", [("zh", ""), ("ja", ""), ("cs", "co znamená 联系?")])
def test_cinske_znaky_se_nechavaji(lang, zprava):
    text = "早餐 7:00"
    assert rg.drop_stray_cjk(text, lang, zprava) == text


def test_bez_azbuky_beze_zmeny():
    text = "Snídaně je od 7:00 do 10:00."
    assert rg.fix_foreign_script(text, "cs", "") is text
