# breaking-past-auto

Pubblicazione automatica di **Breaking Past** su Instagram: un carosello di 8 slide
ogni sera alle **20:30:00 Europe/Rome**, a costo zero, su GitHub Actions + GitHub Pages.

In coda ci sono **16 edizioni**, dal 1 al 16 agosto 2026.

---

## Come funziona in tre righe

1. `episodi/` contiene i PNG 1080x1350 e i testi di ogni edizione, così come escono dal generatore.
2. `bp.py prepare` li converte in JPEG, li valida contro le specifiche Instagram e riempie `queue.json`.
3. Ogni sera un workflow parte in anticipo, dorme fino alle 20:30:00 esatte e pubblica.

Le immagini vivono in `docs/`, servite da GitHub Pages su URL pubblici: **è Meta a
scaricarle dai propri server**, non siamo noi a caricarle. Per lo stesso motivo devono
essere JPEG — l'API Instagram rifiuta i PNG.

---

## Messa in servizio (una volta sola, ~15 minuti)

### 1. Repo e Pages

```bash
cd breaking-past-auto
git init && git add -A && git commit -m "Breaking Past: pipeline di pubblicazione"
gh repo create breaking-past-auto --public --source . --push
```

Poi su GitHub: **Settings → Pages → Build and deployment → Deploy from a branch →
`main` / `/docs`**. Attendi che l'URL pubblico diventi attivo (di solito 1-2 minuti).

Il repo dev'essere **pubblico**: Meta deve poter scaricare le immagini senza autenticarsi.

### 2. Variabili e segreti

**Settings → Secrets and variables → Actions**

| Dove | Nome | Valore |
|---|---|---|
| Variables | `PAGES_BASE_URL` | `https://<tuo-utente>.github.io/breaking-past-auto` |
| Variables | `HASHTAGS_IN_FIRST_COMMENT` | `false` |
| Secrets | `IG_USER_ID` | l'id numerico dell'account Instagram |
| Secrets | `IG_ACCESS_TOKEN` | il long-lived token (60 giorni) |
| Secrets | `GH_TOKEN_SECRETS` | PAT con permesso *Secrets: read and write* su questo repo |

`GH_TOKEN_SECRETS` serve solo al rinnovo automatico del token: il `GITHUB_TOKEN`
standard non ha il permesso di riscrivere un secret.

### 3. Token Instagram

API usata: **Instagram API with Instagram Login** (`graph.instagram.com`).
App in modalità sviluppo, nessuna App Review necessaria per pubblicare sul proprio
account. L'account Instagram dev'essere **professional** (creator o business).

Su developers.facebook.com, nell'app → *Instagram → API setup with Instagram login*:
genera il token, poi scambialo per un long-lived token. `IG_USER_ID` è l'id che
compare nella stessa schermata.

### 4. Verifica

```bash
gh workflow run "Prepara episodi (PNG -> JPEG + queue.json)"
gh workflow run "Pubblica edizione delle 20:30" -f dry_run=true
```

Il `dry_run` esegue preflight e prova ogni URL, senza pubblicare nulla.

---

## Comandi

```bash
python bp.py prepare        # PNG -> JPEG, validazione, alt text, caption, queue.json
python bp.py status         # stato della coda: cosa è uscito, cosa manca, quanta autonomia resta
python bp.py preflight      # token, account, quota, URL raggiungibili, edizione di oggi
python bp.py publish        # pubblica alle 20:30:00 esatte (idempotente)
python bp.py publish --dry-run --date 2026-08-01
python bp.py refresh-token  # rinnova il token e riscrive il secret
python bp.py quota          # quota di pubblicazione residua nelle 24 ore
```

In locale servono `PAGES_BASE_URL`, `IG_USER_ID`, `IG_ACCESS_TOKEN` nell'ambiente.

---

## I tre workflow

| File | Quando | Cosa fa |
|---|---|---|
| `pubblica.yml` | ogni sera, 17:50 e 18:50 UTC | dorme fino alle 20:30:00 e pubblica il carosello |
| `prepara.yml` | a ogni push su `episodi/` | converte, valida, aggiorna `queue.json` e l'indice |
| `rinnova-token.yml` | ogni lunedì, 04:00 UTC | rinnova il token e riscrive il secret |

### Perché due cron per la pubblicazione

Il cron di Actions ragiona in UTC e non conosce l'ora legale italiana. `50 17 * * *`
sono le 19:50 locali d'estate, `50 18 * * *` d'inverno. Girano entrambi tutto l'anno:
quello fuori stagione trova l'orario già passato oltre la tolleranza, oppure
l'edizione già marcata come pubblicata, ed esce senza fare nulla.

Il cron di Actions inoltre **slitta**, anche di parecchi minuti sotto carico. Per
questo il job parte in largo anticipo e l'orario esatto lo decide Python:
`zoneinfo("Europe/Rome")`, ora legale calcolata al volo, `sleep` fino a `20:30:00`.

### Perché non escono mai due post

Tre reti di sicurezza sovrapposte:

- **`concurrency`** sul workflow: un solo job alla volta nel gruppo.
- **`posted: true`** in `queue.json`: controllato prima dell'attesa **e di nuovo dopo**,
  perché nel frattempo un altro job potrebbe aver pubblicato.
- **marcatura immediata** dopo `media_publish`: se il job crolla mentre scrive il
  permalink, l'edizione risulta comunque già pubblicata.

---

## Cosa viene validato in `prepare`

Ogni slide, prima di finire in coda:

- formato **JPEG** (l'API rifiuta i PNG), qualità 90, senza sottocampionamento cromatico
- larghezza tra 320 e 1440 px (oltre 1440 Instagram ricomprime: meglio farlo noi bene)
- rapporto tra 4:5 e 1.91:1 — le nostre 1080x1350 sono esattamente 4:5
- peso sotto gli 8 MB
- **alt text obbligatorio** per ogni slide, letto da `alt_text.txt` e spezzato sui
  marcatori `— SLIDE N —`, massimo 1000 caratteri ciascuno
- **caption presa alla lettera** da `caption.txt`, hashtag compresi: massimo 2200
  caratteri e 30 hashtag
- da 2 a 10 slide per carosello

Se qualcosa non torna, `prepare` lo dice e chiude con exit code 1. Non aggira nulla.

---

## L'interruttore degli hashtag

`HASHTAGS_IN_FIRST_COMMENT` è **`false`**. Le caption restano esattamente quelle di
`caption.txt`. Portandolo a `true`, la coda di hashtag viene staccata dalla caption e
pubblicata come primo commento sul post; se il commento non riesce, il post resta
comunque online e il fatto viene registrato nel log.

---

## Aggiungere edizioni

1. Copia la cartella `Episodio_NNN_AAAA-MM-GG_Titolo/` dentro `episodi/`.
   Deve contenere `NNN_slide_1.png` … `NNN_slide_8.png`, `caption.txt`, `alt_text.txt`.
2. `git push`.
3. `prepara.yml` fa il resto e aggiorna `queue.json` da solo.

La data di uscita è **nel nome della cartella**: non c'è un calendario da tenere allineato
altrove.

---

## Cosa non si può automatizzare

Verificato: nell'API Meta non esistono i parametri. Non è un limite di questo repo, è un
limite della piattaforma, per qualsiasi strumento.

- **La musica sul carosello** (quella che sblocca l'eleggibilità alla scheda Reels).
- **Le Story con sondaggi e sticker.**

Restano manuali. `episodi/*/story.txt` contiene già il testo pronto da incollare.

---

## Se qualcosa va storto

| Sintomo | Causa quasi sempre | Rimedio |
|---|---|---|
| `preflight` dice token non valido | 60 giorni passati senza rinnovi | `gh workflow run "Rinnova il token Instagram"` |
| immagini non raggiungibili | Pages non ancora pubblicato, o `PAGES_BASE_URL` sbagliata | controlla Settings → Pages e la variabile |
| `nessuna edizione in coda per oggi` | finite le edizioni preparate | prepara il batch successivo |
| il post non è uscito | job fallito o cancellato | `gh workflow run "Pubblica edizione delle 20:30"` — l'idempotenza protegge dai doppioni |
| quota esaurita | 50 pubblicazioni in 24 ore | aspetta la finestra successiva |

---

## Il render delle slide

I PNG in `episodi/` **non** vengono rigenerati da questo repo: sono quelli approvati.
Se un giorno servisse rirenderizzarli con `render_carousel.py`, va usato **Inter 4.0**
(`rsms/inter`, cartella `extras/otf/`) e **JetBrains Mono 2.304**: con Inter 4.1 le
metriche dei glifi cambiano quel tanto che basta a far scattare un gradino diverso
dell'auto-fit, e alcune slide si impaginano in modo visibilmente diverso.

Font attesi ai percorsi indicati in `00_Sistema/design_tokens.json`:

```
/usr/share/fonts/opentype/inter/InterDisplay-Black.otf
/usr/share/fonts/opentype/inter/InterDisplay-ExtraBold.otf
/usr/share/fonts/opentype/inter/Inter-{Regular,Medium,SemiBold,Bold}.otf
/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-{Regular,Medium,Bold}.ttf
```

Entrambi i font sono SIL OFL 1.1: uso commerciale consentito.
