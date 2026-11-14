# Opis zewnętrznego systemu EMS — Solar Smart

**Magazyn energii** · mikroinstalacja OZE · falownik hybrydowy SRNE · SolarAssistant · Solar Smart (Raspberry Pi 4)

---

## 1. Przedmiot i zakres

Niniejszy dokument opisuje **funkcję zarządzania energią (EMS)** realizowaną przez zewnętrzny system inteligentnego sterowania przepływem energii elektrycznej pozyskanej z OZE pomiędzy:

- bieżącym zużyciem urządzeń w gospodarstwie domowym,
- magazynem energii elektrycznej,
- siecią elektroenergetyczną.

W instalacji **nie występuje magazyn ciepła**. Łańcuch priorytetów dla tego obiektu ma postać:

**zużycie → magazyn energii → sieć**

---

## 2. Architektura systemu EMS

| Element | Funkcja |
|---------|---------|
| Falownik hybrydowy **SRNE** | Sterowanie przepływami PV, magazynu, obciążenia i sieci |
| **Magazyn energii** | Akumulator podłączony do falownika |
| **SolarAssistant** | Lokalna bramka REST API do falownika (harmonogramy, tryby pracy) |
| **Solar Smart** (Raspberry Pi 4, rok produkcji 2021) | Zewnętrzny EMS: prognozy, plan dobowy, automatyzacja w trybie Smart lub Smart+ |
| Baza historii invertera (**IHDB**) | Dane pomiarowe rzeczywistego zużycia i produkcji |
| **Cloudflare Access** | Zdalny, uwierzytelniony dostęp do interfejsu EMS |

EMS nie jest wyłącznie funkcją wbudowaną w falownik — stanowi **osobny system sterowania** działający na kontrolerze Raspberry Pi i komunikujący się z falownikiem przez SolarAssistant.

---

## 3. Algorytm sterowania — priorytety i ograniczenia

### 3.1. Kolejność priorytetów

1. **Bezpośrednie pokrycie zapotrzebowania** — energia z PV zasila bieżące obciążenie domu.
2. **Magazynowanie energii elektrycznej** — nadwyżka PV po pokryciu obciążenia ładuje akumulator.
3. **Oddanie nadwyżki do sieci** — tylko nadwyżka: PV przy pełnym magazynie albo kontrolowane rozładowanie magazynu do sieci. Warunki ekonomiczne i rezerwa — w rozdziale 4 «Ograniczenie eksportu w godzinach szczytowej produkcji PV».

Na falowniku utrzymywany jest priorytet **Load first** (Solar power priority).

### 3.2. Planowanie co 15 minut

Plan na resztę dnia i na jutro jest liczony co 15 minut z prognozy produkcji, zużycia, stanu magazynu i cen. Zakończone godziny zostają na pomiarach. Bieżąca godzina zachowuje akcję i okno timera ustalone o pełnej godzinie.

1. PV pokrywa obciążenie. Niedobór pokrywa magazyn, a dopiero resztę — import z sieci.
2. Nadwyżka PV ładuje magazyn. Eksport PV do sieci występuje przy pełnym magazynie.
3. Rozładowanie wieczorne po 15:00 zostawia w magazynie energię na dom do godziny, w której prognoza PV przekracza zużycie, nie później niż do **13:00** następnego dnia. Jeśli do 13:00 słońce nadal nie pokrywa domu, o 13:00 w magazynie zostaje minimalny stan naładowania.
4. Eksport z magazynu jest tylko ponad tę rezerwę i tylko gdy cena sprzedaży RCE jest wyższa niż wartość zużycia własnego w taryfie G12 off-peak.
5. Ładowanie z sieci jest w godzinach off-peak, na odbudowę rezerwy albo na niedobór bez PV.

### 3.3. Realizacja na falowniku

| Pozycja | Działanie |
|---------|-----------|
| **Off** | Plan i historia. Brak zapisu na falownik. |
| **Smart** | Na początku godziny zapis okna ładowania albo rozładowania zgodnie z planem. |
| **Smart+** | Te same zapisy co Smart. W tanim oknie porannym dokupowana jest energia, gdy późniejsze godziny mają cenę sprzedaży nie niższą niż próg Super RCE, a następnie jest sprzedawana w tych godzinach. Dokupienie nie podnosi stanu naładowania powyżej 95% na początku pierwszej z tych godzin i nie jest dodawane, gdy magazyn w oknie porannym jest już powyżej 90%. |

Poza zaplanowanym oknem rozładowania falownik pracuje w trybie **Limit power to home load**, co ogranicza niekontrolowany eksport. Przy zaplanowanym rozładowaniu do sieci ustawiany jest tryb **On-grid**.

---

## 4. Ograniczenie eksportu w godzinach szczytowej produkcji PV

Nieplanowany eksport w godzinach szczytowej produkcji PV jest ograniczony przez:

- brak planowania eksportu z magazynu w godzinach dziennych przy typowych niskich cenach RCE względem autokonsumpcji (G12);
- eksport nadwyżki PV do sieci wyłącznie po zapełnieniu magazynu;
- wymuszanie trybu **Limit home** na falowniku poza zaplanowanym oknem rozładowania;
- rezerwę stanu naładowania: wieczorny rozład nie schodzi poniżej energii potrzebnej domowi do pokrycia zużycia przez PV, najpóźniej do 13:00 następnego dnia; bez takiego pokrycia o 13:00 zostaje minimum.

---

## 5. Praca wyspowa (zasilanie awaryjne)

Możliwość **pracy wyspowej** zapewniona jest na poziomie **instalacji elektrycznej**, niezależnie od oprogramowania EMS.

| Tryb pracy | Opis |
|------------|------|
| **Domyślny — przyłączenie do sieci** | Instalacja pracuje on-grid. Przy zaniku napięcia w sieci dystrybucyjnej zasilanie w domu jest wyłączane. |
| **Autonomiczny — ręczne przełączenie** | Operator instalacji przełącza wyłącznik/automat na obwód awaryjny. Gospodarstwo domowe jest zasilane z magazynu energii przez falownik **niezależnie od stanu sieci** elektroenergetycznej. |

Przełączenie w tryb autonomiczny jest świadomą czynnością operatora (hardware). Solar Smart nie steruje automatem wyspowym. Do dokumentacji instalacji dołącza się schemat obwodu awaryjnego oraz procedurę przełączenia.

---

## 6. Podsumowanie

Zewnętrzny system EMS **Solar Smart** na Raspberry Pi współpracuje z falownikiem SRNE przez SolarAssistant. Priorytet przepływu to zużycie, potem magazyn, potem sieć. Eksport w szczycie produkcji PV jest ograniczony. Praca wyspowa jest zapewniona na poziomie instalacji elektrycznej.
