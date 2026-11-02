# Huawei Band 11 BLE protocol (Phase 1)

What Band Bridge must speak to a Huawei Band 11 with no phone in the loop.
Everything here is ported from Gadgetbridge (AGPL-3.0) and checked against two
real Band 11 Gadgetbridge logs.

## 0. Sources and confidence

| Source | What it gave |
|---|---|
| Gadgetbridge `master` @ `b4f1eb5a` (2026-11-02) | All formats and algorithms. Paths below are relative to `app/src/main/java/nodomain/freeyourgadget/gadgetbridge/`. |
| Gadgetbridge issue #6012 log, Band 11 FW `6.0.0.113` | Full first pairing + reconnects, raw hex both directions. **Primary evidence.** |
| Gadgetbridge issue #5930 log, Band 11 FW `6.0.0.118` | Reconnects (bind only), raw hex. |
| huawei-lpv2 (Python, Band 4 era) | Structure only. Its auth does not apply to Band 11. |

Local copies: `../reference/gadgetbridge/` (sparse clone) and `../reference/issue-logs/gb6012.log`, `gb5930.log`. These are outside the repo and not committed.

Confidence tags used below:
- **[log]** seen on a real Band 11 in the logs.
- **[src]** read from Gadgetbridge source, not visible in the logs.
- **[verify]** our assumption; must be checked on the real band.

---

## 1. GATT layout [log]

| Item | UUID | Properties | Use |
|---|---|---|---|
| Huawei service | `0000fe86-0000-1000-8000-00805f9b34fb` | | Main protocol |
| Write | `0000fe01-0000-1000-8000-00805f9b34fb` | **writeNoResponse** | PC → band |
| Notify | `0000fe02-0000-1000-8000-00805f9b34fb` | notify | band → PC |
| (unused) | `0000fe05-...` / `0000fe06-...` | writeNoResponse / notify | Second channel. Gadgetbridge ignores it on BLE. |

Other services on the band: GAP `1800`, GATT `1801`, Battery `180f`, Heart Rate `180d`, HID `1812`, `3802`, `cc353442-be58-4ea2-876e-11d8d6976366`. None are needed.

Base UUID constant: `service/btle/AbstractBTLEDeviceSupport.java` (`BASE_UUID`). Service/char constants: `devices/huawei/HuaweiConstants.java`.

Connect order (from `HuaweiSupportProvider.initializeDevice`):
1. Connect, discover services.
2. Enable notifications on `FE02` (CCCD `0x2902`).
3. Send the Link Params request (section 5, step 1).

### OS-level pairing [log]

- Gadgetbridge does **not** bond at the OS level (`HuaweiLECoordinator.getBondingStyle() = BONDING_STYLE_NONE`). The band's link-layer security is replaced by HiChain (section 6).
- During the first HiChain exchange, the band sent an SMP pairing request. Gadgetbridge rejected it (`BluetoothPairingRequestReceiver - Aborting unwanted pairing request`, log line 194), and pairing still succeeded.
- **The 30-second drop [log, both Gadgetbridge logs + our Phase 4 run]:** the band sends that SMP pairing request **only during HiChain op 1 (first pairing), never on op-2-only reconnects**. The first-pairing connection then **always drops exactly 30 s later**, which is the Bluetooth SMP timeout:
  - gb6012: `14:02:50.188 → 14:03:20.212`, `14:03:24.648 → 14:03:54.535`
  - gb5930: `20:18:53.020 → 20:19:22.988`
  - ours (Windows): approve at `19:03:35.9` → drop at `19:04:05.995`

  This is the #6012 "GATT_VALUE_NOT_ALLOWED loop". Gadgetbridge never resets its `firstConnection` flag, so every auto-reconnect re-runs op 1, which triggers SMP again and another 30 s drop. **For us:** save `auth_token` right after op 1 step 3, expect the drop, and reconnect with op 2 only. That reconnect gets no SMP request and stays up. No OS pairing is needed.

### Device name

Advertised as `HUAWEI Band 11-XXX` (e.g. `HUAWEI Band 11-2BF`). Gadgetbridge matches `huawei band 11-.*` case-insensitively; note `huawei band 11 pro-` also matches that prefix.

---

## 2. Transport: chunking and reassembly

- **Write chunking [log]:** Link Params returns `mtu = 0x0014` (20). Gadgetbridge splits every outgoing frame into 20-byte writes (`requests/Request.java`, `doPerform`). Example: a 49-byte frame (`LEN = 0x2C`) goes out as 20 + 20 + 9 bytes (log lines 55-60). Before Link Params arrives, mtu defaults to 65535, so the first request (16 bytes) goes out whole.
- **Notification reassembly [log]:** the band also sends 20-byte notifications. A frame is complete when `3 + length + 2` bytes have arrived (section 3). Buffer partial data, and keep any trailing bytes after a frame as the start of the next one (`HuaweiPacket.parseData`, `getLeft()`).
- **Windows (Phase 2, real band) [log]:** Windows negotiates ATT MTU 517. The band then reports Link Params `mtu = 0x0202` (514 = 517 − 3) instead of 20, and sends its 58-byte reply as **one** notification. So the band's MTU follows the ATT MTU. Set the write chunk size from the Link Params `mtu` (Phase 2 still uses 20; the first request fits either way). The reassembler must handle both 1-notification and multi-notification frames.
- **Interval:** Link Params tag `0x04` = `0x0000` on Band 11, so there is no wait between requests.
- **Write type:** `FE01` is writeNoResponse only → bleak `write_gatt_char(..., response=False)`.
- **Ordering:** Gadgetbridge serializes all writes through one queue. Do the same, and never interleave chunks of two frames.

### "Force legacy GATT" (issue #6012 fix) [src]

In `service/btle/BtLEQueue.java`, `connectionForceLegacyGatt` changes two things:

| | Default (Android ≥ 8) | Force legacy GATT |
|---|---|---|
| connect call | `connectGatt(ctx, false, cb, TRANSPORT_LE, phyMask, handler)` with `phyMask = PHY_LE_1M_MASK \| PHY_LE_CODED_MASK` | `connectGatt(ctx, false, cb, TRANSPORT_LE)`: no PHY preference |
| callback thread | dedicated `HandlerThread` (`BtLEQueue_N_in`) | Binder thread (no extra handler) |

In the #6012 log the default path ended up on 2M PHY (`phy updated: tx=2 rx=2`).

**Windows mirror:** bleak/WinRT exposes no PHY preference, so we never request one. That already matches legacy mode. Process all notifications on one asyncio loop, in order. Nothing else needs mirroring.

---

## 3. Frame format [src + log]

Source: `devices/huawei/HuaweiPacket.java` (`serializeUnsliced`, `serializeSliced`, `parseData`).

### Unsliced frame

```
5A | LEN (u16 BE) | 00 | SVC | CMD | TLV... | CRC16 (u16 BE)
```
- `LEN` = number of bytes after the LEN field, **excluding** the CRC (= 1 + 2 + len(TLV)).
- CRC = **CRC-16/XMODEM** (poly `0x1021`, init `0x0000`, no reflection, no xorout) over every byte before the CRC, including `5A`. Gadgetbridge: `util/CheckSums.getCRC16(data, 0x0000)`.

Worked example, the Link Params request [log]:
```
5A 000B 00 01 01  01 00  02 00  03 00  04 00  F13B
```
CRC verified: `crc16_xmodem(5A000B0001010100020003000400) = 0xF13B`.

### Sliced frame (body larger than `sliceSize`)

```
5A | LEN | SLICE | FLAG | [SVC CMD, first slice only] | chunk | CRC16
```
- `SLICE`: `01` first, `02` middle, `03` last. `FLAG`: slice counter `00, 01, 02, ...`.
- `LEN` = bytes after LEN excluding CRC. Each slice is at most `sliceSize` bytes in total.
- Gadgetbridge slices once `2 + len(body) > sliceSize − 7`, so the largest unsliced frame is `sliceSize − 1` bytes (1021 on Band 11). None of the 483 frames in `gb6012.log` is sliced; slicing is tested only against Gadgetbridge's unit-test vectors.
- Each slice has its own CRC. On receive, concatenate the chunks (first chunk starts with SVC CMD) and parse once `SLICE == 03`.
- Band 11 `sliceSize = 0x03FE` (1022) [log]. Notifications and auth never get near that; slicing matters only for big payloads.

### Non-TLV bodies

`(0x0a,0x05)`, `(0x28,0x06)`, `(0x2c,0x05)`, `(0x1c,0x05)` carry raw bytes, not TLV. We never use them.

---

## 4. TLV encoding [src]

Source: `devices/huawei/HuaweiTLV.java`.

```
TAG (1 byte) | LEN (VarInt) | VALUE (LEN bytes)
```
- **VarInt:** big-endian base-128. Every byte except the last has bit 7 set. Examples: `5 → 05`, `127 → 7F`, `128 → 81 00`, `500 → 83 74`.
- **Containers:** the value is itself a TLV list. By convention the tag has bit 7 set (`0x81`, `0x84`, `0x8C`, `0x8D`, `0x82`, `0x83`, `0x90`, `0x91`).
- **Value types**, all big-endian: `byte` → 1 byte, `boolean` → 1 byte (`00`/`01`), `short` → 2, `int` → 4, `long` → 8, `String` → UTF-8, no terminator.
- **Empty tag** (`put(tag)` with no value) = "please send me this field" in requests.
- **Parse quirk:** a trailing single `00` byte at the very end is ignored. It appears after some decryptions.
- **Result/error tag** `0x7F`: an int. `0x000186A0` (100000) = success; anything else is an error code.

---

## 5. Band 11 connection sequence [log]

Taken from `gb6012.log` lines 1-760 (first pairing) and `gb5930.log` (reconnect). `E` = TLV body encrypted (section 7).

| # | SVC/CMD | E | Name | When |
|---|---|---|---|---|
| 1 | `01/01` | no | Link Params | always |
| 2 | `01/33` | no | Security Negotiation | always |
| 3 | `01/2C` | no | Get PIN code | **first pairing only** |
| 4 | `01/28` | no | HiChain, operationCode 1 (auth), steps 1-4 | **first pairing only** (user confirms on band) |
| 5 | `01/28` | no | HiChain, operationCode 2 (bind), steps 1, 2, 4 | always |
| 6 | `01/07` | yes | Product info | always |
| 7 | `01/05` | yes | Set time | always |
| 8 | `01/08` | yes | Battery level | always |
| 9 | `01/02` | yes | Supported services | always |
| 10 | `01/03` | yes | Supported commands (repeats per batch of services) | always |
| 11 | `01/37` | yes | Expand capabilities | always |
| 12 | many | yes | "dynamic services" (settings, weather, P2P, notifications, ...) | always |

Timing on the real band: steps 1-5 took 4.3 s on first pairing, including the user confirming on the band. Reconnects (steps 1, 2, 5) took under 1 s.

### Step 1: Link Params `01/01`

Request: tags `01 02 03 04` all empty (+ `06` for AW devices only).

Band 11 response [log]:
```
5A 0035 00 01 01
  01 01 02                     protocolVersion = 2
  02 02 03FE                   sliceSize = 1022
  03 02 0014                   mtu = 20
  04 02 0000                   interval = 0 ms
  05 12 00 01 <16-byte nonce>  [1] = authVersion = 1, [2..17] = serverNonce
  07 01 04                     deviceSupportType = 4  -> HiChain3
  09 01 00                     bondState = 0
  0D 01 00   0F 01 01   10 01 00   (unused by Gadgetbridge)
1A41
```
Tag `0x0C` (encryptMethod) is **absent** → 0. Tag `0x08` (authAlgo) is absent → 0.

### Step 2: Security Negotiation `01/33`

`isHiChain()` is true for deviceSupportType 4, so `authMode = 4`.

Request [log]:
```
01 01 04                 authMode = 4
02 01 01                 1 = "not a reconnect" (always sent as 1)
05 20 <32 ASCII bytes>   our deviceUUID = androidId (32 uppercase hex chars)
03 01 01
04 01 00
06 00                    (authMode 4 only)
07 NN <phone model>      (authMode 4 only; Gadgetbridge sends Build.MODEL, e.g. "A063")
[0D 01 01]               only if encryptMethod == 1 (not on Band 11)
```

Band 11 response [log]:
```
01 01 04   02 01 02   03 01 01   04 01 00
05 40 <64 ASCII hex chars>    06 12 <18 x 00>    07 00    08 01 00
09 12 "F8:3B:7E:06:22:BF" 00  (band BT MAC as text, NUL-terminated)
0A 04 00000000
```
**Our band on Windows (Phase 3, factory-reset, never paired) [log]:** tag2 = `01` (not `02`), so `authType = 1 XOR 4 = 5`. That is still HiChain3 ((5 XOR 1) == 4). Tag2 likely means "pair type": 1 = first pair, 2 = known peer. The #6012 capture came from a band stuck in a re-pair loop. [verify after Phase 4 that a reconnect returns 2]

Auth type math (`DeviceConfig.SecurityNegotiation.Response.parseTlv`): tag1 = 4 sets `pw = 4`; tag2 = 2, so `authType = 2 XOR 4 = 6`. Then `isHiChain3(6)` is true ((6 XOR 2) == 4), so the path is **HiChain mode** (`GetHiChainRequest`). If tag `0x7F` were present it would override authType.

### Step 3: PIN code `01/2C` (first pairing only)

Request: `01 00`. Nothing shows on the band screen for this request (Phase 3, real band). The pairing prompt comes later, at HiChain op 1 step 1. The response carries an **encrypted PIN**, not a code for the user to type:
- `01` = ciphertext (80 bytes on Band 11), `02` = IV (16 bytes).
- Decrypt (`HuaweiCrypto.decryptPinCode`): AES-128-**CBC**/PKCS7 (encryptMethod 0), key = `DIGEST_SECRET_v1` (authVersion 1), IV = tag `02`.
- `DIGEST_SECRET_v1 = 70 FB 6C 24 03 5F DB 55 2F 38 89 8A EE DE 3F 69`

### Steps 4-5: HiChain, see section 6.

### Steps 6-11

| SVC/CMD | Request TLV | Notes |
|---|---|---|
| `01/07` | tags `01 02 07 09 0A 11 12 16 1A 1D 1E 1F 20 21 22 23`, all empty | Response: `03` hw ver, `07` sw ver, `09` serial, `0A` model, `0F` pkg, `11` name |
| `01/05` | `01` int: unix time, `02` short: tz offset | `HuaweiUtil.getTimeAndZoneId` |
| `01/08` | `01` empty | Response `01` = battery % |
| `01/02` | `01` = list of service IDs we know | Response `02` = one byte per service (`1` = supported) |
| `01/03` | `81 { 02 svc, 03 [cmd ids] }...` | Response `81 { 02 svc, 04 [1/0 per cmd] }...` |
| `01/37` | `01` empty | Response `01` = capability **bitmap**, LSB-first: cap `n` = `bytes[n/8] & (1 << n%8)` |

Band 11 expand capabilities [log]: `FF7F014226C37442A0A6BC7222698A4B87AAA10D6EFDCC8A8F511901594DA48A0A42100000`

| Cap bit | Meaning (`HuaweiState.java`) | Band 11 |
|---|---|---|
| 73 | notification reply actions | yes |
| 77 | notification timestamp / icon | yes |
| 82 | canned (quick) replies | **yes** |
| 89 | notification reply | **yes** |
| 94 | repeated notify | yes |
| 120 | remove single notification | yes |

### Required post-init setup [log, Phase 5 diagnostic]

**Without these messages the band revokes the session about 25 s after connect.** Every later request, even plaintext ones, then gets a plaintext `7F = 0x000186A4` while the link stays up. With them, requests keep working (checked at +30, +45, +70 and +100 s). Plaintexts below are taken from Gadgetbridge's log for Band 11 and sent in this order, encrypted:

| SVC/CMD | Plaintext TLV | Reply on our band |
|---|---|---|
| `1A/05` extended account | `01=00, 03=01` | `02=00` |
| `01/31` setting related | tags `01`–`06` empty | `01=0F 02=0E 03=03 …` |
| `01/30` accept agreements | `81 { 82 {03 name, 04=01, 05="20230508-20230508-0-0", 06=<now ms as ASCII>} ×3 }`, names: `software_update_service_statement`, `device_information_management`, `user_license_agreement` | `7F=186A0` |
| `01/3F` reverse capabilities | `01=FDF7737A` | `7F=186A0` |
| `01/3E` set-up device status | `01=01, 02=<band name>, 03=00` | **no reply** (send without waiting) |
| `01/3D` wear status | `01` empty | `01=02` |
| `01/35` connect status | `01=01` | `7F=186A0` |

Which of these is the one that matters was not bisected; we send all of them, like Gadgetbridge. Afterwards the band starts sending `01/10` phone-info requests (answer them), `34/01` P2P data (ack it), and many `37/01`, `37/02`, `37/04` service announcements (no answer needed so far).

**For Band Bridge** we need steps 1-8, the setup messages above, and the notification subset of step 12 (section 8). Supported services/commands/capabilities (9-11) are cheap and tell us what the band accepts, so keep them too.

### Band-initiated messages after init [log]

**Our band, FW `6.0.0.130(SP3C00M03)`, model `Candy-B19FB` (Phase 4) [log]:**
- First pairing: after init it sent `37/02` (`hw.wearable.httpProxy` → `hw.unitedevice.httpProxy`), `01/3D` (tag1 = 2, then 1), `37/01` (`com.huawei.watch.home` → `com.huawei.wearengine`) twice, and `25/03` (tag1 = `64`). Reconnects only sent `25/03` about 25 s in.
- None of these was answered and the link stayed up 12 min. No `01/10` or `34/01` arrived, because we skip Gadgetbridge's dynamic-services init.
- Battery `01/08` works during init. **Every later `01/08` (from about 60 s on) gets a plaintext `7F = 0x000186A4` reject**, yet the link stays up. Gadgetbridge only polls every 15 min, so its logs don't show this. Cause unknown (see section 11). Any reply still proves liveness.

In the Gadgetbridge logs, right after init the band sends: `01/10` (phone info request), `37/xx`, `0F/xx` (weather), `34/01` (P2P), `08/08`, `25/01` (music), `32/02`. Gadgetbridge answers `01/10` (`AsynchronousResponse.handlePhoneInfo`, `GetPhoneInfoRequest`) and acks every P2P data/ping (section 8.4). **[verify]** which of these the band needs answered to stay connected. Start by answering `01/10` and P2P and logging the rest.

---

## 6. HiChain authentication (Band 11 path)

Source: `service/devices/huawei/requests/GetHiChainRequest.java`, `devices/huawei/packets/DeviceConfig.java` (`HiChain`), `util/CryptoUtils.java`.

### 6.1 Identity we must persist

| Field | How it is made | Gadgetbridge pref |
|---|---|---|
| `android_id` | 16 random bytes → **uppercase** hex → 32 ASCII chars. Our "device id". | `PREF_FAKE_ANDROID_ID` |
| `auth_token` | Returned by the band in op 1, step 3. Long-term pairing secret. | `authkey` |

`authIdSelf` = the 32 ASCII bytes of `android_id`. Every hex string on the wire is **uppercase** (`GB.hexdump`).

### 6.2 Packet wrapper `01/28`

Request TLV (all steps):
```
01 <JSON string, compact, no spaces>
02 <operationCode: 01 auth | 02 bind>
03 <requestId: u64 BE, = current time in ms, fixed for the whole operation>
```
Response TLV [log]: `01` JSON, `02` opcode, `03` requestId, `04` type, `05` 00.
- `04 == 00`: JSON step response. Route by payload key: `isoSalt` → step 1, `returnCodeMac` → step 2, `encAuthToken` → step 3.
- `04 != 00`: step 4 / finished.
- A JSON `payload.errorCode != 0` is a failure.

### 6.3 JSON envelope

Keys in this insertion order (Android `JSONObject` keeps order; mirror it with a Python dict):
```json
{"authForm":0,
 "payload":{"version":{"minVersion":"1.0.0","currentVersion":"2.0.16"}, ...step fields...},
 "groupAndModuleVersion":"2.0.1",
 "message": <step | 0x10 if op 2>,
 ...op-1-only fields...,
 ...isDeviceLevel (op 2, steps 1-2)...}
```
Op 1 only, appended to the envelope: `"requestId":"<ms as string>"`, `"groupId":GROUP_ID`, `"groupName":"health_group_name"`, `"groupOp":2`, `"groupType":256`, `"peerDeviceId":android_id`, `"connDeviceId":android_id`, `"appId":"com.huawei.health"`, `"ownerName":""`.

Op 2 only: `"isDeviceLevel":false` appended in steps 1 and 2. Payload step 1 adds `"pkgName":"com.huawei.devicegroupmanage"`, `"serviceType":GROUP_ID`, `"keyLength":32`.

`GROUP_ID = "7B0BC0CBCE474F6C238D9661C63400B797B166EA7849B3A370FC73A9A236E989"`

Real op 1 step 1 request, payload part [log, line 117]:
```
{"version":{...},"isoSalt":"2AB8...F882","peerAuthId":"4142...3632","operationCode":1,"seed":"611C...2BEC","peerUserType":0}
```

### 6.4 Steps

Notation: `H(k,m)` = HMAC-SHA256, `SHA` = SHA-256, `HKDF(ikm,salt,info,32)` = RFC 5869 HKDF-SHA256 (Gadgetbridge's `hkdfSha256` is standard), `GCM` = AES-GCM with a 128-bit tag.

| Step | We send (payload) | Band replies (payload) | We compute / check |
|---|---|---|---|
| 1 | `isoSalt=HEX(randSelf[16])`, `peerAuthId=HEX(authIdSelf)`, `operationCode`, `seed=HEX(seed[32])`, `peerUserType=0` (+ op 2 extras) | `isoSalt`=randPeer, `peerAuthId`=authIdPeer, `peerUserType`, `token` | `key = SHA(ASCII(HEX(pin)))` for op 1, `key = auth_token` for op 2. `psk = H(key, seed)`. Check `token == H(psk, randPeer‖randSelf‖authIdSelf‖authIdPeer)` |
| 2 | `peerAuthId=HEX(authIdSelf)`, `token=HEX(H(psk, randSelf‖randPeer‖authIdPeer‖authIdSelf))` | `returnCodeMac` | Check `returnCodeMac == H(psk, 00000000)` |
| 3 (op 1 only) | `sessionKey = HKDF(psk, randSelf‖randPeer, "hichain_iso_session_key")`. `challenge[16]`, `nonce[12]` random. `nonce=HEX(nonce)`, `encData=HEX(GCM_enc(sessionKey, nonce, aad="hichain_iso_exchange", challenge))` | `nonce`, `encAuthToken` | `auth_token = GCM_dec(sessionKey, nonce, aad=challenge, encAuthToken)` → **persist** |
| 4 | `nonce=HEX(n[12])`, `encResult=HEX(GCM_enc(sessionKey, n, aad="hichain_iso_result", 00000000))`, `operationCode` | type != 0 | op 1: start op 2 with a new requestId. op 2: `secretKey = HKDF(sessionKey, randSelf‖randPeer, "hichain_return_key")` |

- Op 2 skips step 3: it derives `sessionKey` the same way, then sends the step-4 payload with `message = 0x13`. Gadgetbridge takes `messageId` before skipping the step [log: op 2 messages are `0x11, 0x12, 0x13`; op 1 messages are `1, 2, 3, 4`].
- `secretKey` (32 bytes) is the **per-connection transport key** (section 7). It is not persisted.
- **Physical step:** on op 1 step 1 the band shows a pairing prompt. The user taps ✓ on the band. Gadgetbridge allows 30 s for this (`firstAuthenticateTimeout`), and 5 s per step otherwise.
- **Reconnect:** with a stored `auth_token`, skip steps 3-4 of section 5 and go straight to op 2. If op 2 fails (band was reset or forgot us), delete `auth_token` and redo op 1.

### 6.5 Not used by Band 11

`HiChainLite` (authMode 2: `GetAuthRequest` `01/13` + `BondParams` `01/0F` + `Bond` `01/0E`), "normal mode", and Honor PAKE/STS. Do not port these. If a future firmware changes deviceSupportType away from 4, the Link Params log line will show it.

---

## 7. Transport encryption (after HiChain) [src + log]

Source: `HuaweiTLV.encryptRaw/decryptRaw`, `HuaweiPacket.ParamsProvider.getIv`, `HuaweiCrypto.encrypt`.

Every packet whose class does not set `isEncrypted = false` has its whole TLV body replaced by:
```
7C 01 01                    encrypted = true
7D 10 <IV, 16 bytes>
7E <VarInt len> <ciphertext || 16-byte GCM tag>
```
For Band 11 (deviceSupportType 4):
- Cipher: **AES-GCM** (because deviceSupportType == 4, even though encryptMethod == 0), no AAD, 128-bit tag.
- Key: `secretKey` from HiChain op 2 (32 bytes → AES-256).
- IV: 16 fresh random bytes per packet. Python `cryptography`'s `AESGCM` accepts 16-byte nonces.

Real example [log line 55]: `5A 002C 00 01 08 | 7C0101 | 7D10 894A…E313 | 7E12 CE70…B8E4 | A5B6`. That is the battery request: plaintext `01 00` (2 bytes) + 16-byte tag = 18-byte ciphertext.

Incoming: if tag `7C` is present and true, decrypt `7E` with IV `7D` and re-parse the TLV.

Plaintext before auth: Link Params, Security Negotiation, PIN, HiChain.

Other devices (not Band 11): AES-CBC/PKCS7 with IV = 12 random bytes + 4-byte BE counter (counter starts from BondParams tag `09`).

---

## 8. Notifications and quick replies

Sources: `devices/huawei/packets/Notifications.java`, `service/devices/huawei/requests/SendNotificationRequest.java`, `service/devices/huawei/HuaweiNotificationsManager.java`, `service/devices/huawei/p2p/HuaweiP2PCannedRepliesService.java`, `devices/huawei/packets/P2P.java`.

### 8.1 Setup (service `0x02`)

| SVC/CMD | Request | Purpose |
|---|---|---|
| `02/05` | `01` empty | Notification capabilities. Response `01` byte. |
| `02/02` | `01` empty | Constraints. Band 11 [log]: content max **500** chars (type 1), title 120 (type 2), others 30-60. Gadgetbridge forces content format = `02`. |
| `02/04` | `81 { 02 bool, 03 bool }` | Notifications on/off. Send `true`. |
| `02/08` | `01 bool` | "Wear message push": value is the **inverse** of the "no notifications when not worn" switch. Send `01` (= push even when not worn). **[verify]** |

### 8.2 Send a notification `02/01`

```
01 short   msgId            our running counter
02 byte    notificationType 0x7F generic (chat apps map to generic), 0x02 sms, 0x0F email
03 bool    01               (legacy "vibrate")
84 { 8C { 8D {0E textType, 0F encoding, 10 text}... } }
           textType: 03 title, 02 sender, 01 body; encoding = 02 (from constraints)
11 string  sourceAppId      app package name, max 127 chars
12 string  category         e.g. "msg"                         (optional)
18 string  replyKey         present only if cap 89; non-empty = reply-capable
15 int     unix seconds     if cap 77
19 string  notificationKey  if cap 94 or 120
1A int     notificationId   if cap 94 or 120
1B string  channelId        if cap 94 or 120
32 long    "when" (ms)      optional
```
Body longer than the content limit: Gadgetbridge cuts to `limit - 13` and appends `...`.

`notificationKey` default format: `"0|<sourceAppId>|<id>||0"`.

**Reply button.** In Gadgetbridge the band offers replies when it has cap 89 and tag `0x18` is non-empty. Gadgetbridge fills `0x18` with the notificationKey only when the Android notification has a reply action. There is no app-ID whitelist in Gadgetbridge. The firmware might still gate replies by app [verify]. Phase 6 plan:
1. `notificationType = 0x7F`, `sourceAppId = "org.telegram.messenger"`, `category = "msg"`, `replyKey = notificationKey = "0|org.telegram.messenger|<askId>||0"`.
2. If no reply button appears, try `com.whatsapp`, then type `0x02` (sms) with `14` subId / `17` address.

### 8.3 Reply event from the band `02/10`

```
01 type      1 = SMS (key is a phone number), 2 = notification (key = our replyKey)
02 encoding  3 = UTF-16 text, otherwise UTF-8
03 subId
04 key       echoes our tag 0x18
05 addData
06 text      the chosen quick reply
```
Map `key` → our `askId`. Gadgetbridge acks only type 1, with `02/10 { 07 FF }` (FF = OK, 7F = error). For type 2 no ack is sent [src; verify the band does not retry].

### 8.4 Quick-reply list (P2P service `0x34/01`)

Band 11 has cap 82 [log]. The list lives in a P2P module.

P2P frame `34/01` TLV:
```
01 byte    cmdId: 1 ping, 2 data, 3 ack, 4 get-version
02 short   sequence (our running counter; the band echoes it)
03 string  src package
04 string  dst package
05 string  src fingerprint     (cmdId 2 only)
06 string  dst fingerprint     (cmdId 2 only)
07 bytes   data                (inner TLV)
08 int     code                (cmdId 3 only; we send 4 bytes, the band sends 1 byte)
```
Quick-reply module constants:

| | Value |
|---|---|
| our src (module) | `hw.unitedevice.smsquick` |
| dst package | `com.huawei.watch.home` |
| our fingerprint | `UniteDeviceManagement` |
| dst fingerprint | `603AC6A57E2023E00C9C93BB539CA653DF3003EBA4E92EA1904BA4AAA5D938F0` |

Inner data TLV:
- Query: `01 int 7001` → the band answers with cmdId 2 data `01 int 7001, 82 { 83 { 04 "reply" }... }`.
- Update (**what we send**): `01 int 7002, 82 { 83 {04 "1"}, 83 {04 "2"}, 83 {04 "3"}, 83 {04 "yes"}, 83 {04 "no"}, 83 {04 "continue"}, 83 {04 "stop"} }`. The list must not be empty.
- 7003 "connect": Gadgetbridge avoids it because it may wipe the saved replies.

Band 11 [log, lines 2009-2204]:
1. Our query: cmdId 2, seq `000F`, data `01 04 00001B59` (7001).
2. Band data: cmdId 2, seq `0005`, data `01 04 00001B59 82 00` (an empty list on a fresh band).
3. Our ack: cmdId 3, seq `0005`, code `000000CF`.
4. Band ack of our query: cmdId 3, seq `000F`, code `CF`.

**Rule:** every band→PC cmdId 1 or 2 must be acked with cmdId 3, the same sequence, swapped src/dst, and code `0x000000CF`.

Slot count: Gadgetbridge assumes 10 (a TODO in its source). We need 7.

### 8.4b Verified on our band (Phase 6, FW 6.0.0.130) [log]

- Setup replies:
  - `02/05` → `01=01`
  - `02/02` → content limit 500
  - `02/04` and `02/08` → `7F=186A0`
- The quick-reply update was acked with `CF`, and reading the list back returned all 7 items.
- `02/01` with `sourceAppId = org.telegram.messenger`, type `0x7F` and a reply key → `7F=186A0`. The band showed the reply UI, so **no WhatsApp/SMS fallback is needed**.
- The tap arrived about 9 s later as `02/10`: `01=02` (type 2), `02=02` (UTF-8, not 3), `04=` our key echoed exactly, `06="yes"`. No ack was sent, and the band did not retry.
- `02/06` remove → `7F=186A0`.
- Per-question lists (`/ask` with options) were verified on 2026-11-02. A 7002 update with the option texts plus yes/no/continue/stop (8 items) was acked with `CF` in about 0.15 s. The band showed the words, and the tap came back as the plain text ("Purple"). Restoring the default list after the answer was also acked. We assume at most 10 slots and 30 characters per reply; neither limit has been tested.

### 8.5 Remove a notification `02/06`

`01 byte type, 02 str sourceAppId, 03 str notificationKey, 04 int notificationId, 05 str channelId, [06 str category]`. Useful to clear a question after it is answered or times out.

## 8b. Music control (service `0x25`) [src: MusicControl.java, HuaweiMusicManager.java; log]

| Message | Direction | Body |
|---|---|---|
| `25/01` | band → PC | The band's music screen opened. Body: empty `01`, or `7F`. |
| `25/01` | PC → band | Ack `7F=186A0`, then push `25/02`. |
| `25/02` | PC → band | Now playing: `01` artist, `02` title (UTF-8), `03` state u8 (Android PlaybackState: 0 none, 1 stopped, 2 paused, 3 playing), `04` max volume u8, `05` volume u8. The band answers `7F=186A0`. |
| `25/03` | band → PC | Button: `01` (lowest byte; 1 play, 2 pause, 3 previous, 4 next, 5 volume up, 6 volume down) and/or `02` volume. Ack `7F=186A0` and push `25/02` again. The values 64 and 100 mean "left the screen" and get no ack. |

No setup is needed. `25/04` and `25/0D` in Gadgetbridge are only for uploading music files. Verified on 2026-11-02 (FW 6.0.0.130):
- An unprompted `25/02` sent on connect is accepted. Each time the music screen opens, the band sends `25/01` and accepts our `25/02`. UTF-8 Arabic titles display.
- Buttons arrive as `25/03 01=` 01 play, 02 pause, 03 previous, 04 next.
- **Volume is never sent as buttons 5/6.** The band sends its slider as `02=` the new level on the scale of our `04` max (we report 100), in steps of 6. It runs **past 0 as a signed byte** (`04`, `FE`, `F8`, ...), so read it signed and clamp. Band Bridge sets the Windows master volume (IAudioEndpointVolume) and reports the real level in `05`.
- Windows' `TrySkipNext/Previous` returns True even when the player ignores it. Chrome does that for a YouTube video outside a playlist. Check `controls.is_next_enabled` / `is_previous_enabled`.
- Band Bridge's mapping: a single previous/next tap seeks 10 s back/forward, and a second tap within 1.5 s changes the track.

## 8d. Weather (service `0x0F`) [src: Weather.java; log]

The PC sends this sequence, each request encrypted and answered with `7F` or data:

| Cmd | Body | Band 11 reply (2026-11-02) |
|---|---|---|
| `09` start | `01=03` | `7F=186A0` |
| `05` unit | `01=` 0 °C, 1 °F | `7F=186A0` |
| `02` support | `01` empty | `01=FF` (all: icon, wind, pm2.5, low/high, location, current temp, unit, AQI) |
| `06` extended support | `01` empty | `01=000F` |
| `0A` sun/moon support | `01` empty | `01=00000003` (sunrise/sunset, moon phase) |
| `01` current | `81{02 icon, 03 u16 wind}`, `85{06 low, 07 high}`, `08` name, `09` temp (signed, always °C), `0A` unit, `0C` time u32, `0E` source, `0F` uv, `10` humidity, `11` wind km/h u32, `12` feels-like | `7F=186A0`; shows on the band |
| `08` forecast | `81{82{03 ts, 04 icon, 05 temp, 06 rain %, 07 uv}...}`, `90{91{12 ts, 13 icon, 14 high, 15 low, 16 sunrise, 17 sunset}...}` | **`7F=1C139`, rejected.** Gadgetbridge gets the same code from a Band 11 (issue log gb6012). |

Band-initiated messages:
- After connect the band sends `0F/04 {01=02, 02=00}`, which is acked with `04 {01=186A0}`.
- `0F/04` with `7F=186AA` means "send weather".
- `0F/03 {01=0001}` is unknown, and Gadgetbridge ignores it.

## 8c. Heart rate: not available [src + log]

`07/1C {01=bool}` (Gadgetbridge `EnableRealtimeHeartRate`) is **not** a live reading. Gadgetbridge sends it in place of `07/17` (automatic HR) when "realtime mode" is set: it switches **continuous HR monitoring** on or off. On 2026-11-02, with the switch on and `2A37` (standard Heart Rate Measurement) subscribed for 40 s, no reading arrived; the band sent only `07/0F {01=03, 02=00000000}`. Gadgetbridge has no live HR read for Huawei, so Band Bridge does not offer one. Never send `07/1C 00`: it turns off the user's monitoring.

---

## 9. Keys and config to store (`config/band.json`, gitignored)

```json
{
  "address": "F8:3B:7E:06:22:BF",
  "android_id": "32 uppercase hex chars",
  "auth_token": "hex, from HiChain op 1 step 3",
  "firmware": "6.0.0.113"
}
```
Never log `auth_token` or the per-connection `secretKey` except at an explicit debug level. Raw frames are logged in hex, and HiChain JSON reveals the token exchange but not the key.

---

## 10. Known stability issues (Gadgetbridge)

- **#5930**, FW 6.0.0.118, "disconnects after a few seconds". Cause: the Huawei Health app on the phone was also connecting to the band. **For us:** the phone must not be connected to the band (unpair it, or turn off its Bluetooth / Huawei Health). The band talks to one central at a time.
- **#6012**, FW 6.0.0.113, `STATE_DISCONNECTED GATT_VALUE_NOT_ALLOWED` about 20 s after connect, followed by a new pairing prompt in a loop. It resolved by itself for the reporter. Gadgetbridge maintainers report "Force legacy GATT" fixes similar cases (section 2). The reporter's later state: occasional drops, sometimes stuck offline until Bluetooth was toggled.
- **Measured recovery (Phase 5, our band, Windows) [log]:**
  - PC Bluetooth toggled off/on: ready again in 13 s.
  - Band powered off for 3 min: five `BleakDeviceNotFoundError` retries (each scan times out at 30 s; backoff 2-30 s), then ready 7 s after the band booted.
  - The band **keeps our pairing across a reboot**: Security Negotiation tag2 = `02`, op 2 only, no prompt.
- **Implications for Phases 4-5:** no PHY request (already true on Windows); one strictly serialized write queue; a reconnect loop with backoff; detect "connected but no reply to Link Params within N s" as stuck and force a disconnect. Gadgetbridge's only periodic traffic is a battery poll (`01/08`) every 15 min. Use it as our heartbeat.

---

## 11. Open questions to verify on the real band

1. ~~Does declining the OS pairing keep the link?~~ Answered: no. The first-pairing link always drops 30 s after the SMP request, and reconnects never get one (section 1).
2. Does the band need answers to `01/10` phone info and other band-initiated requests to stay connected (section 5)?
3. Does the band show the reply UI for a non-whitelisted `sourceAppId` (section 8.2)?
4. Reply event: encoding value and exact key echo on Band 11 (section 8.3).
5. Is the bleak/WinRT negotiated ATT MTU irrelevant? We chunk at 20 regardless.
6. Why does `01/08` battery return `0x186A4` from about 60 s after init? Do other encrypted commands (notifications `02/01`) still work after that point? Test first thing in Phase 5/6.

## 12. Test vectors for Phase 3

- Gadgetbridge unit tests (`app/src/test/.../devices/huawei/`): `TestHuaweiPacket`, `TestHuaweiTLV`, `TestVarInt`, `TestHuaweiCrypto`, `packets/TestNotifications`.
- Real frames from `gb6012.log`: Link Params TX/RX (section 5), Security Negotiation TX/RX, PIN TX, encrypted battery TX (section 7). CRCs of these were verified with CRC-16/XMODEM during Phase 1.
- HiChain cannot be replayed from the logs: random values and the PIN-derived key are not logged. Test the HMAC/HKDF/GCM helpers with RFC vectors and round-trips instead.
