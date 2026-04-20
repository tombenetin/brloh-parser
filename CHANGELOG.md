# Changelog

Všetky dôležité zmeny v projekte **brloh-parser**.

## [0.2.0] - 2026-03-31
### Fixed
- vypnuté `product_disappeared` eventy pre Brloh
- odstránená príčina opakovaných po sebe idúcich duplicitných `disappeared` eventov

### Reason
- zdrojový listing Brlohu nie je dostatočne stabilný pre spoľahlivú detekciu zmiznutia produktu
- parser preto sleduje len relevantné zmeny: nové produkty, ceny a dostupnosť

## [0.1.9] - 2026-03-31
### Added
- nekolidujúci scan loop cez systemd service
- ďalší scan sa spustí až 120 sekúnd po dobehnutí predchádzajúceho
- systemd service uložená aj v repozitári pod `systemd/brloh-parser-scan.service`

### Verified
- frontend beží na lokálnej app URL
- frontend HTML obsahuje aktuálnu verziu a auto-reload mechanizmus po dokončení scanu

## [0.1.8] - 2026-03-31
### Changed
- celý parser prepnutý na skladový listing `https://www.brloh.sk/Vyhladavanie/pokemon-karty?query=tcg#s=r&st=1`
- parser je odteraz orientovaný na skladové produkty z tohto výpisu

### Fixed
- databáza sa resetuje kvôli odstráneniu starých neaktívnych produktov a historických záznamov

## [0.1.7] - 2026-03-31
### Changed
- source listing prepnutý na URL filtrovanú len na skladové produkty
- parser načítava produkty zo skladového výpisu `#s=r&st=1`

## [0.1.6] - 2026-03-31
### Fixed
- parser prechádza listing cez „Zobraziť ďalšie produkty“
- načítanie produktov už neostáva len na prvej stránke výsledkov
- detail parsing produktov zostáva zachovaný pre správnu cenu, dostupnosť a obrázok

## [0.1.5] - 2026-03-31
### Changed
- parser skúšal prechod stránok cez `#pg=N` na search výpise

## [0.1.4] - 2026-03-31
### Changed
- parser prešiel na detailné čítanie produktových stránok namiesto parsovania celej listing karty
- zlepšená presnosť cien a dostupnosti

## [0.1.3] - 2026-03-31
### Fixed
- odstránené chyby v JavaScripte vo `page.evaluate()`
- stabilizovaný scraping zo search/listing stránky

## [0.1.2] - 2026-03-31
### Changed
- zjednodušený DOM parsing pre Brloh
- úpravy source URL a heuristiky pre názov, cenu a dostupnosť

## [0.1.1] - 2026-03-31
### Fixed
- odstránené príliš agresívne filtre, ktoré zhadzovali validné produkty
- upravený baseline parser po prvom debugovaní

## [0.1.0] - 2026-03-31
### Added
- prvá funkčná verzia brloh-parsera
- zachovaný frontend, API kontrakt a štýl podľa pikazard-parser
- baseline parser pre BRLOH.sk
- extrakcia názvu produktu
- extrakcia ceny
- extrakcia dostupnosti
- extrakcia URL produktu
- extrakcia obrázka do payloadu
