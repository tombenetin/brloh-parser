# Changelog

Všetky dôležité zmeny v projekte **brloh-parser**.

## [0.1.6] - 2026-03-31
### Fixed
- parser prechádza listing cez „Zobraziť ďalšie produkty“
- načítanie produktov už neostáva len na prvej stránke výsledkov
- detail parsing produktov zostáva zachovaný pre správnu cenu, dostupnosť a obrázok

### Changed
- verzia zjednotená vo frontende a v backende
- changelog doplnený o všetky doterajšie verzie

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
