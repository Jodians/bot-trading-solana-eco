# Studi Wallet Profitabel (on-chain) — temuan & implikasi ke konfigurasi bot

Dibuat oleh `tools/wallet_study.py`. Semua angka di bawah berasal dari data
on-chain nyata (Helius + RPC publik), bukan estimasi.

## Ringkasan eksekutif

Bot saat ini punya **exit yang praktis tidak pernah aktif**. Dari 305 round-trip
nyata milik dua wallet profitabel:

| Ambang bot sekarang | Frekuensi tercapai |
|---|---|
| `TAKE_PROFIT_MULTIPLE=2.0` | **4,3%** |
| `STOP_LOSS_MULTIPLE=0.5` | **0,3%** |

Artinya ~95% posisi tidak pernah keluar via TP maupun SL. Yang benar-benar
menutup posisi adalah `MAX_HOLD_SEC=900` — jadi strategi bot yang sebenarnya
berjalan bukan "TP 2x / SL 0,5x", melainkan **"pegang 15 menit lalu jual di harga
apa pun yang kebetulan muncul"**. Ini kemungkinan besar bukan yang Anda maksud.

## Metodologi & keterbatasannya

Solscan tidak bisa dipakai dari mesin ini (403 Cloudflare; `pro-api` butuh token
berbayar). PnL direkonstruksi dari RPC mentah:

1. Kandidat dari leaderboard kolscan (1d/7d/30d) — 87 wallet unik.
2. `getSignaturesForAddress`, buang signature yang `err`-nya terisi.
3. `getTransaction` → delta SOL (native + WSOL, fee dibalikkan bila wallet yang
   bayar) + delta token per tx.
4. FIFO lot matching per (wallet, mint) → realized PnL, hold time, exit multiple.

**Validasi silang:** win rate KOREAN hasil rekonstruksi = 37,7%, angka kolscan =
40%. Selisih 2,3 poin, jadi metodenya waras.

### Yang TIDAK bisa dijawab data ini

- **Tidak ada jalur harga intra-trade.** Kita hanya tahu harga keluar akhir, bukan
  apakah posisi sempat turun ke 0,7x sebelum naik ke 1,5x. Konsekuensinya: setiap
  simulasi SL di bawah ini **terlalu optimis**, karena SL ketat di dunia nyata
  akan memotong sebagian pemenang saat mereka dip. SL tidak bisa diputuskan dari
  data ini — hanya arahnya yang jelas.
- **Window 700 tx memotong sejarah.** Mr.Frog punya 223 `sell_without_entry`:
  penjualan token yang belinya di luar window. Posisi seperti itu dibuang, bukan
  dihitung setengah.

## Wallet: 2 dari 4 target bisa dianalisis

| Wallet | Round-trip | Status |
|---|---|---|
| Mr.Frog | 151 | ✅ dipakai |
| KOREAN | 154 | ✅ dipakai |
| Schoen | 0 | ❌ tidak bisa |
| yeekidd | 4 | ❌ tidak bisa |

**Kenapa Schoen & yeekidd gagal** (bukan bug, ini sifat wallet-nya): fee payer
transaksinya orang lain, delta SOL wallet = 0,0, tapi wallet muncul sebagai owner
di `tokenBalances`. Program yang dipakai `proVF4pMXVaYqmy4NjniPh` +
`spl-associated-token-account`. Pola `token_in_no_sol`: Schoen 313, yeekidd 493.
Token masuk/keluar tanpa SOL bergerak di tx yang sama — ciri wallet
distribusi/bundler atau multi-wallet farm, bukan wallet eksekusi trade. PnL-nya
tidak ada di sini, jadi tidak bisa direkonstruksi dengan metode apa pun yang
hanya melihat satu wallet.

**Peringatan lebih luas:** wallet KOL paling terkenal justru paling mustahil
dilacak. Cented & Nyhrox: 1000 signature terakhir 100% gagal, rentang 0,3–1,5
menit (~3.750 tx/menit). Itu bukan trade mereka — itu bot copy-trade orang lain
yang gagal, yang menyebut alamat mereka via address lookup table. Cupsey / decu /
trunoest / Kadenox: ok 0–3%.

## Profil perilaku (305 round-trip gabungan)

```
expectancy    1,1476x per trade     median 1,069x
≥2,0x          4,3%                 ≤0,50x   0,3%
≥1,5x         11,8%                 ≤0,65x   4,6%
≥1,3x         22,3%                 ≤0,80x  15,1%
entry         p10 0,94 | p50 2,00 | p90 2,74 SOL
hold          p50 13s | p75 47s | p90 6.648s | p95 19.157s
scale-out     36,1% posisi dijual bertahap
pump.fun      87–90% dari semua posisi
```

### Temuan 1 — hold time bimodal, dan yang lambat jauh lebih menguntungkan

Mr.Frog, dipisah di ambang 60 detik:

| | n | PnL | win | expectancy | hold median | sell median |
|---|---|---|---|---|---|---|
| cepat ≤60s | 84 | 21,46 SOL | 92,9% | 1,1104x | 9s | 1 |
| lambat >60s | 67 | **66,14 SOL** | 95,5% | **1,3964x** | 5.931s | 2 |

76% profitnya datang dari trade lambat, dengan expectancy 26% lebih tinggi.
`MAX_HOLD_SEC=900` akan **memotong seluruh kelompok itu** (median hold-nya 99
menit). Ini konflik konfigurasi paling mahal yang saya temukan.

KOREAN sebaliknya: 149 dari 154 trade ≤60s, expectancy hanya 1,0616x. Dua wallet
ini menjalankan strategi berbeda; jangan campur setelannya.

### Temuan 2 — win rate tinggi bukan sumber profit

Wallet "selalu profit" di leaderboard justru win rate-nya rendah: Cupsey 43%,
KOREAN 40%, West 46%, Casino 24%, Megga 39%. Tidak ada yang mendekati 100%.
Profit datang dari payoff asimetris, bukan akurasi.

Konsentrasi profit: KOREAN top-5 trade = 71,1% dari total PnL (satu trade
terbaik = 19,8%). Mr.Frog jauh lebih merata, top-5 = 18,2%. Model Mr.Frog lebih
bisa direplikasi bot kecil; model KOREAN butuh menang besar sesekali.

### Temuan 3 — sizing tetap, bukan berdasarkan keyakinan

Mr.Frog: entry p10 2,00 / p50 2,50 / p90 3,00 SOL. Nyaris konstan. Scale-in cuma
4,6% — dia tidak menambah posisi. Tapi scale-out 43,7% — keluar bertahap.
Asimetri ini disengaja: masuk sekali, keluar dicicil.

## Rekomendasi konfigurasi (belum diterapkan)

```diff
- TAKE_PROFIT_MULTIPLE=2.0     # tercapai 4,3% → praktis mati
+ TAKE_PROFIT_MULTIPLE=1.4     # tercapai ~17%, di atas median 1,069x

- STOP_LOSS_MULTIPLE=0.5       # tercapai 0,3% → tidak melindungi apa pun
+ STOP_LOSS_MULTIPLE=0.75      # wallet nyata memotong di 0,64–0,82x

- MAX_HOLD_SEC=900             # memotong justru kelompok paling profitable
+ MAX_HOLD_SEC=5400            # 90 menit, menampung kelompok "lambat"

  BUY_AMOUNT_SOL=0.1           # biarkan; sizing tetap sudah benar
  MAX_OPEN_POSITIONS=3         # naikkan hanya setelah exit terbukti jalan
```

Dasar TP 1,4: pada grid exit gabungan, menaikkan TP memang menaikkan edge
nominal (TP 3,0 = 16,93% vs TP 1,3 = 7,92%), **tetapi** grid itu mengabaikan
jalur harga — TP tinggi hanya menguntungkan kalau posisi benar-benar sampai ke
sana, dan hanya 4,3% yang sampai 2x. TP 1,4 dipilih karena berada di atas median
realized (1,069x) tapi masih dalam jangkauan ~17% trade, sehingga TP benar-benar
menjadi mekanisme keluar aktif, bukan hiasan.

**Yang belum bisa saya rekomendasikan dengan yakin: SL.** Tanpa jalur harga
intra-trade, angka 0,75 adalah tebakan terdidik dari titik potong wallet nyata
(0,64–0,82x), bukan hasil optimasi. Untuk memutuskannya benar, bot perlu mencatat
harga minimum tiap posisi selama paper run — itu pekerjaan terpisah.

### Yang perlu perubahan kode, bukan cuma .env

Scale-out (36% trade wallet nyata, dan pada Mr.Frog median 2 sell untuk kelompok
paling profitable) **tidak bisa dikonfigurasi** — `snipe.py` menjual 100% posisi
dalam satu kali di `decide_exit()`. Menambahkan exit bertahap (misal jual 50% di
1,3x, sisanya trailing) adalah perubahan paling berdampak yang tersisa, dan itu
butuh patch pada monitor posisi.

## Cara menjalankan ulang

```bash
# triage: wallet mana yang layak dianalisis
python tools/wallet_survey.py <kolscan_leaders.json>

# studi PnL (cache per-signature, run ulang gratis)
python tools/wallet_study.py '[["nama","alamat"]]' 700 0 tools/out.json
```

Butuh `HELIUS_API_KEY` valid di `.env`. **Catatan penting:** `getHealth` dan
`getSlot` tidak memotong kredit, jadi key yang kuotanya habis tetap menjawab
"ok". Untuk mengetes key, pakai method yang memakan kredit seperti
`getSignaturesForAddress`. Batas praktis free tier di mesin ini: 4 worker
paralel (pada 5 worker → 23 dari 80 request kena 429; pada 10 worker → 57 dari
80 kena 429).
