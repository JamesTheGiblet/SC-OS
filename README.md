# SC-OS — Semantic Capsule System

Agents exchange **capsules**: typed, signed, validated, content-addressed units of meaning.
A capsule carries claims with confidence, relations between entities, what the sender
doesn't know, where the claim came from, and how urgently to act on it.
The "OS" is the kernel that routes capsules between agents; the capsule protocol is the core.

**Status: v0.1 plus multi-node sessions, self-description and learning rules.** A server node
handles several peers at once over TCP, with signed capsules, pinned keys, replay protection and a
SQLite ledger. Rules are capsules too: they fire on incoming capsules and gain or lose trust from
reported outcomes. SC-OS also records its own code, docs and test results as capsules. The kernel
has run with three nodes on one machine and across two: a Windows PC and an Android phone (Termux)
over Wi-Fi. An M5StickC PLUS2 takes part as an edge device: it runs only the stripped protocol, and
a gateway on the PC signs for it.
Read [the good, the bad, and the ugly](#the-good-the-bad-and-the-ugly) before building on it.
[ONE_PAGER.md](ONE_PAGER.md) sums it all up on one page.

## Why capsules

A plain JSON message tells you what someone said, not how far to believe it. A capsule makes
that part of the message: each claim has a confidence, evidence and optionally an expiry, the sender lists
what it doesn't know, and provenance says what the capsule was derived from. Capsules are
content-addressed, so the same capsule has the same digest on every node. That lets any node
spot duplicates and replays, and follow `derived_from` chains across machines. Each node's
ledger stores the signature with the capsule, proving who said what without the original
traffic. Trust comes from outcomes: receiving a claim isn't evidence that it's true. An agent's
opinion moves only when acting on something works or fails, and fades back to "unknown" when
left untested (Leighton Weight). For ESP-NOW radios, a single-claim capsule shrinks to a stripped
form and upgrades back to a full capsule at the gateway.

## Quick start

Requires Python 3.10+.

```sh
pip install -r requirements.txt
python demo.py                     # single-process walkthrough, 11 steps
python -m store store/demo.db      # print the ledger the demo wrote (add --full for JSON)
```

`demo.py` deletes `store/demo.db` at startup so each run begins empty.
Digests differ between runs because every capsule gets a fresh UUID and timestamp.

### SC-OS describes itself

```sh
python self_describe.py                         # read the code and docs, run the tests, store capsules
python self_describe.py --show                  # print the current self-description
python self_describe.py --show self.module      # one topic
python self_describe.py --no-tests --dry-run    # preview without running tests or storing
```

The system records what it's made of as capsules from `agent://sc-os`, signed and stored in
`store/self.db`, each validated like any received capsule:

| Topic | One capsule per | Claims |
| --- | --- | --- |
| `self.identity` | the system | what SC-OS is and why, size, dependencies, git commit |
| `self.module` | Python file | purpose, classes with method signatures, functions, constants; `depends_on` relations to local modules and packages |
| `self.tests` | test file | every test it defines; `supports` relations to the modules it tests |
| `self.test_results` | test file | PASSED or FAILED from actually running it |
| `self.vocabulary` | the system | intents, triggers, claim types, predicates, provenance methods |
| `self.decisions` | the system | design decisions from `NOTES.md`, as directive claims |
| `self.open_questions` | the system | open questions from `NOTES.md`, as known unknowns |
| `self.limits` | README section | the bad and the ugly |
| `self.next` | the system | planned next steps |

Capsule ids come from content, so a rerun with nothing changed stores nothing. Change a file and
only its capsule gets a new version, with `derived_from` pointing at the one it replaces. Evidence
cites file, line and file hash. Capsules live 7 days (the schema maximum): self-knowledge that
isn't refreshed expires. `--no-tests` leaves earlier test results alone.

### Rules that learn

```sh
python -m rules list     --node alice                      # rules, trust, whether each fires
python -m rules issue    --node alice rules/builtin.json   # sign and store rules (--force revives forgotten ones)
python -m rules maintain --node alice                      # forget faded rules, re-issue live ones
```

A rule is a capsule on topic `rule.<name>`, signed by the node that runs it. Its directive claim
holds a JSON spec: a `when` pattern over incoming capsules and the capsules to emit `then`.

```json
{
  "name": "verify-high-confidence-risk",
  "when": { "topic": "*_risk", "trigger": "threshold", "min_confidence": 0.8 },
  "then": [{ "intent": "request", "to": "{from}", "trigger": "task", "topic": "{topic}",
             "claims": [{ "type": "directive", "statement": "verify: {claim}" }] }]
}
```

- **Patterns** match on `topic` and `from`/`to` (globs), `intent`, `trigger`, `claim_type`,
  `min_confidence` and relation `predicate`. Placeholders in outputs: `{from}` `{to}` `{topic}`
  `{id}` `{claim}` `{confidence}` `{rule}`.
- **Outputs explain themselves.** Every emitted capsule has `provenance.method = "rule"` and
  `derived_from = [rule id, input id]`. A rule fires at most once per input, never on its own
  node's capsules, and never on another rule's output, so rules can't loop.
- **Only a node's own rules run.** A rule capsule from a peer is stored, never executed.
  `RuleEngine.adopt()` re-issues it as the node's own, starting at unknown. A tampered or
  forged rule in the ledger fails its signature check and is skipped.
- **Rules learn from outcomes.** When a rule asks an agent for a task, that agent answers with a
  `task_result` carrying `outcome: {status: success|failure}`. The outcome counts only if it
  answers a task this node sent, comes from the agent it was sent to, and hasn't been counted
  before. It moves the rule's opinion (+0.1 and weight 1 for success, −0.2 and weight 3 for
  failure). A rule whose value falls to 0 or below stops firing.
- **A rule lives as long as its trust.** `maintain()` re-issues live rules before their 7-day
  capsule TTL runs out. A tested rule is forgotten once its decayed weight falls below 0.05; an
  untested rule gets 30 days. Forgotten rules stay forgotten across restarts unless issued with
  `--force`. Editing a rule makes a new rule (new id, `derived_from` the old one) that starts at unknown.

`run_alice.py` issues `rules/builtin.json` at start and maintains rules hourly.
`run_bob.py` carries out a task a rule asks for and reports `--outcome success|failure|ignore`.
Trust lives in the node's database (`opinions` table), never in the rule capsule and never on the wire.

### Multiple nodes: Alice, Bob, Carol

Alice first, then any number of clients, each in its own terminal:

```sh
python run_alice.py                           # serves many peers on 127.0.0.1:7707 until Ctrl+C
python run_bob.py                             # agent://bob: hello, capsule, wait for ACK
python run_bob.py --name carol --hold 2       # agent://carol, waits 2 s so sessions overlap
python run_bob.py --name carol --replay       # then resends the same signed capsule; Alice must reject it
python run_bob.py --outcome failure           # report failure for the task Alice's rule asks for
```

All accept `--host` and `--port`. `run_alice.py --sessions N` exits after N sessions end.
Clients retry the connection for 10 seconds, then say what to check. Keys, pins and ledgers are
found next to the scripts, so it doesn't matter which directory you start them from. A session goes:

1. Bob sends a hello carrying his public key. Alice pins it, or checks it against her pin.
2. Alice answers with her own hello and key. Bob does the same.
3. Bob sends a signed capsule. Alice verifies, validates and replay-checks it, then runs it
   through the scheduler.
4. Alice sends a signed ACK that points back to Bob's capsule. Bob verifies it.
5. If one of Alice's rules fires, she also sends Bob a task. Bob carries it out and returns a
   `task_result` with an outcome, which Alice's rule learns from.

Every connection starts with exactly one hello in each direction. That's deliberate: a peer
may have upgraded its versions since it last connected, and the hello is where its key is checked
against the pin. Several hellos in a ledger mean several sessions.

Alice runs one thread per connection, sharing one node (key, pins, ledger) and one scheduler.
Replies are routed by `to` through the table of open sessions. An agent can have one open
session at a time, and any rejection closes that session only. Logged timings cover signing,
sending, verifying and dispatch, not just network time.

### Two machines

Same code on both. On the server machine:

```sh
python run_alice.py --host 0.0.0.0            # prints: other machines can connect to: 192.168.1.20:7707
```

Allow the port through that machine's firewall (on Windows, accept the prompt for Python on
private networks). On the other machine, give the address once:

```sh
python run_bob.py --host 192.168.1.20
# or put it in peers.json next to run_bob.py, then plain `python run_bob.py`:
#   {"peers": ["192.168.1.20:7707"]}
```

Both sides log the other's clock offset on every hello, with a warning above 5 s. Capsules created more than
30 s in the receiver's future are rejected, so sync clocks (NTP) before blaming the network.
First contact pins each side's key; a machine that regenerates its key (a new `keys/` directory)
is rejected until the other side deletes that pin.

### Edge devices through a gateway

An edge device speaks the stripped format over a local link; `run_gateway.py` turns each device
into its own agent with its own signed session to Alice. Start Alice first, then:

```sh
python run_gateway.py --stdio               # no device: type frames on stdin, read frames on stdout
python run_gateway.py --serial COM5         # a device on USB serial (pip install pyserial)
```

One JSON frame per line:

```text
device -> gateway  {"src":"m5-a1b2c3","cap":{"v":"1.0","id":"r1","to":"alice","i":"inform","t":"tilt_risk","c":0.9,"s":"tilted 40 degrees","tr":"threshold"}}
gateway -> device  {"dst":"m5-a1b2c3","cap":{"v":"1.0","id":"3f2a…","to":"m5-a1b2c3","i":"ack",…,"re":"r1"}}
gateway -> device  {"dst":"m5-a1b2c3","cap":{"v":"1.0","id":"9c41…","to":"m5-a1b2c3","i":"request","t":"tilt_risk","s":"verify: tilted 40 degrees","tr":"task"}}
device -> gateway  {"src":"m5-a1b2c3","cap":{"v":"1.0","id":"r2","to":"alice","i":"inform","t":"tilt_risk","c":1,"s":"checked","tr":"task_result","re":"9c41…","o":"success"}}
```

- **`re`** names the message this one answers, by short id. **`o`** (`success` | `failure`) and
  optional **`od`** (detail) are required on a `task_result` and refused anywhere else.
- **The gateway is the trust boundary.** Devices don't sign. The gateway holds a key per device
  (`keys/<src>.ed25519`) and sends as `agent://<src>`, so Alice's rule sends its task to that
  agent and counts the outcome only from it, exactly as for Bob.
- **The gateway rejects** malformed frames, names that aren't `[a-z0-9-]` (16 characters at most),
  messages for anyone but Alice (no relaying), a `task_result` whose `re` isn't a task it
  forwarded to that device (or has expired), and a message id that device already sent.
- **Sessions come and go.** Alice closes idle sessions after 30 s; the device's next frame opens a
  new one. Short-id tables belong to the device, so a result can arrive on a later session.
- **Devices describe their sensors; the gateway writes the capsule.** A stripped message can't
  carry a sensor description, so a device sends a compact list and the gateway checks it
  (`edge/sc_sensors.json`: fields present, ids unique, `min ≤ margin_low ≤ margin_high ≤ max`)
  and builds a `__sensors__` capsule, signed as the device's agent:

  ```text
  device -> gateway  {"src":"m5-96c048","sensors":[{"id":"battery","type":"voltage","bus":"adc","pin":"38","unit":"V","min":0,"max":5,"margin_low":3.3,"margin_high":4.35,"sample_ms":200},…],"absent":["mic: …"]}
  gateway -> device  {"dst":"*","cmd":"describe"}      (sent when the gateway opens the port)
  ```

  One claim per sensor, statement `sensor:<id>` (the key its opinion will use), one evidence string
  per field (`unit=V`, `margin_low=3.3`, …). Hardware the device has but can't read (`absent`)
  becomes known unknowns. `edge.sensors.parse_evidence` reads a claim back into numbers.

### An M5StickC PLUS2 as the edge device

`firmware/m5stickc_plus2/` runs on the stick under MicroPython, over USB serial to the gateway.
Tilt it past 40°: it reports `tilt_risk` as a threshold, Alice's verify rule sends a task back,
and the red LED blinks. Press **A** (front) if the tilt was real, **B** (side) if not; the outcome
goes to Alice. It asks one question at a time: no new report while a task waits. Stand it under
20° to re-arm. Button C (power, short press) turns the backlight on and off.

The screen (landscape, 240×135) shows:

- a header with the device name and the time from its real-time clock
- tilt in large type: green when armed, yellow past 40°, grey until re-armed
- accelerometer (g) and gyroscope (°/s) on three axes, the IMU's die temperature, the ESP32's own
  temperature (large fixed offset: read it as a trend) and battery voltage
- the link to Alice (report sent, ack, task received, result sent)
- the waiting task in a yellow box with `A = yes  B = no`, and the last outcome sent (green or red)

The buzzer beeps twice when a task arrives, chirps when you send a success and gives a low tone for
a failure, without pausing the loop.

At boot, and whenever the gateway asks, the stick describes its eight sensors (accel, gyro, tilt,
imu_temp, chip_temp, battery, clock, buttons) and Alice stores the `__sensors__` capsule. Tilt's
`margin_high` is the 40° report threshold. The margins come from `sensors.py` in the firmware until
an operator's `__setup__` capsule supplies them. Readings themselves are shown only on the device.
Other hardware on the stick:

| Part | State |
| --- | --- |
| SPM1423 PDM microphone (clock 0, data 34) | Not read. It answers when clocked, but MicroPython's I2S has no PDM input, and counting its data edges didn't track loudness. Needs a PDM-capable build or Arduino firmware. |
| IR transmitter (pin 19, shared with the red LED) | Pulses whenever the LED blinks; no codes are sent. The ESP32's RMT peripheral could send real remote-control codes. |
| Grove port (pins 32, 33) | Free for external sensors; nothing attached. |

One-time setup (erases the stick; back up first if you want the factory firmware back):

```sh
pip install esptool pyserial
python -m esptool --port COM4 read-flash 0 ALL firmware/backup/factory.bin     # optional, 8 MB, git-ignored
python -m esptool --port COM4 erase-flash
python -m esptool --port COM4 --baud 460800 write-flash 0x1000 ESP32_GENERIC-SPIRAM-<date>-v1.29.0.bin
```

The image is the generic ESP32 SPIRAM build from micropython.org. Then copy the firmware, and run:

```sh
python firmware/m5stickc_plus2/deploy.py COM4       # copies the firmware, sets the clock, resets the stick
python run_alice.py --host 0.0.0.0
python run_gateway.py --serial COM4
```

`deploy.py` exists because `mpremote` toggles DTR/RTS when it opens the port, which resets this
board before its REPL can be reached; `run_gateway.py` holds both lines low for the same reason.
The device names itself from its MAC (`m5-96c048`) and prints `# …` notes for a person, which the
gateway skips. `sctalk.py` holds the protocol with no hardware imports, so the tests run it on the PC.
`st7789.py` drives the screen and pushes only rows that changed; between rows the firmware reads
the serial link, so a redraw can't delay an incoming task. `sensors.py` reads the IMU, clock,
battery, chip temperature and buttons, and plays the buzzer. `deploy.py` also sets the stick's clock from the PC's local time.

### Where state lives

| Path | Contents | In git |
| --- | --- | --- |
| `store/<name>.db` | SQLite ledger: every capsule sent or received, with signatures; plus the node's opinions (rule trust and topic opinions) and counted outcomes | no |
| `peers.json` | Where `run_bob.py` connects when no `--host` is given | no |
| `store/gateway.db`, `keys/<device>.ed25519`, `store/<device>.pins.json` | The gateway's ledger, and the key and pins it holds for each edge device | no |
| `store/<name>.pins.json` | Agent id → pinned public key. Delete to forget a peer. | no |
| `keys/<name>.ed25519` | The node's private key | no |
| `store/self.db`, `keys/sc-os.ed25519` | SC-OS's self-description and the key that signs it | no |

Ledgers and pins persist, so later runs add to them and peers must present the same keys.
The run log says `first contact, key pinned` or `key matches pin`. Ledgers from before the
SQLite switch import with `python -m store import store/<name>.log store/<name>.db`, keeping
stored times and signatures, and with them replay protection.

Query a ledger directly:

```sh
sqlite3 store/alice.db "SELECT stored_at, sender, receiver, topic, intent, pubkey_id FROM capsules ORDER BY seq"
sqlite3 store/alice.db "SELECT json_extract(capsule, '$.semantics.claims[0].statement') FROM capsules WHERE topic = 'supply_chain_risk'"
```

### Verified with real processes

- **Restarts:** same keys are accepted. A Bob with a new key is rejected
  (`key for agent://bob changed since first contact`). Deleted pins lead to a fresh first contact.
- **Three nodes:** Bob and Carol's sessions overlapped, and each got the ACK for its own capsule.
  Carol was pinned on first contact while Bob matched his pin.
- **Learning rules:** Alice's `verify-high-confidence-risk` rule asked Bob and Carol to verify
  their reports. Bob reported success (value +1.10, weight 1), Carol failure (+0.90, weight 4), Bob
  success again (+1.00, weight 5). An ignored task taught nothing. Trust persisted across restarts.
- **Replay:** Carol resent a byte-identical signed capsule, and Alice rejected it
  (`replay: capsule … was already received`) and closed that session. A capsule Bob sent before
  the SQLite migration was also rejected when replayed against the migrated ledger.
- **Two machines (2026-09-17):** Alice on a Windows 11 PC (`--host 0.0.0.0`), `agent://phone` on an
  Android phone in Termux, over the phone's Wi-Fi hotspot. Round trips took 15–34 ms, measured on the phone. The phone's
  clock ran about 1.0–1.2 s behind the PC's; once both logged offsets, Alice reported
  `clock offset -1.0 s` and the phone `+1.0 s`, the same gap from either side.
  - First contact pinned both keys; every later run from the phone matched its pin.
  - A byte-identical replay from the phone was rejected and the session closed.
  - Success moved the topic opinion and rule up by 0.1; failure moved both down by 0.2 and added
    weight 3, as the model says.
  - Ctrl+C on the phone mid-session: Alice logged the disconnect at once, and the phone reconnected
    straight away.
  - Airplane mode mid-session (no close sent): Alice logged `connection error: timed out` about 30 s
    later and kept serving. The phone reconnected after Alice was restarted, so reconnecting inside
    the same Alice process after a silent drop wasn't directly observed.
  - The phone and a PC client (`agent://bob`) had open sessions at the same time; Bob's whole
    session ran while the phone's was waiting, and each got its own ACK and task.
  - Rule trust and topic opinions survived Alice restarts and kept learning.
    `verify-high-confidence-risk` became the first rule to reach `trusted`, earned from outcomes
    reported by Bob, Carol and the phone: +1.70, weight 21.97, 16 outcomes. Topic
    `supply_chain_risk` reached +1.60.
- **A real edge device (2026-09-17):** an M5StickC PLUS2 (ESP32-PICO-V3-02, MicroPython 1.29) on
  USB serial, through `run_gateway.py`, to Alice on the same PC, as `agent://m5-96c048`.
  - Tilt reports from its IMU fired Alice's verify rule, the task came back to the stick, and
    button presses returned outcomes that Alice counted.
  - Success moved topic `tilt_risk` up 0.1; failure (button B) moved it and the rule down 0.2 and
    added weight 3. A success at the +2.0 maximum added weight only. The same rule now holds
    evidence from Bob, Carol, the phone and the stick (+1.70, weight 36.97).
  - Three problems showed up only on the device and were fixed: frames the device ignored after
    the port was reopened (it now parses from the first `{`); an ACK and a task arriving back to
    back were lost, most likely overflowing its small input buffer (it now waits on input instead of sleeping, and the
    gateway leaves 50 ms between frames); and new tilt reports replaced a task still waiting (it
    now asks one question at a time).

### Tests

```sh
python tests/test_store.py          # 15: round-trip, signatures, find, pruning and disk space, import, concurrent writers
python tests/test_transport.py      # 9:  framing, many peers at once, concurrent sends (real localhost TCP)
python tests/test_peer.py           # 17: pinning, rejections, replay, session binding, concurrent sessions
python tests/test_rules.py          # 19: patterns, firing and provenance, own rules only, outcomes, lifetime
python tests/test_self_describe.py  # 6:  valid capsules, every file described, versions chain, rerun stores nothing
python tests/test_weight.py         # 14: the curve, success/failure/idle trajectories, stepwise ticks, sharing rule, domains
python tests/test_validator.py      # 13: every rejection code: schema, version, clock skew, vocab, coherence, provenance, outcome
python tests/test_interpreter.py    # 13: exact wire round-trip, stable digests, expiry, actionable, merge
python tests/test_scheduler.py      # 14: routing, ledger, replies, verified outcomes only, opinions survive restart
python tests/test_network.py        # 6:  peers.json, clock offset, any working directory, session over a network address
python tests/test_gateway.py        # 13: edge outcome fields, device outcome teaches Alice's rule, rejections, session drop, firmware protocol, sensor lists
python -m pytest tests              # all 139
```

## What a capsule looks like

Wire form (as produced by `interpreter.to_wire`, validated by `sc.schema.json`):

```json
{
  "capsule_version": "1.0",
  "id": "urn:uuid:…",
  "created": "2026-09-16T21:11:03+00:00",
  "from": "agent://alice",
  "to": "agent://bob",
  "intent": "inform",
  "trigger": "threshold",
  "semantics": {
    "topic": "supply_chain_risk",
    "claims": [{ "statement": "Supplier X has 40% capacity reduction",
                 "type": "observation", "confidence": 0.82,
                 "evidence": ["source:reuters-2026-09-14"], "valid_until": "…" }],
    "relations": [{ "subject": "SupplierX", "predicate": "affects", "object": "ProductY" }],
    "uncertainty": { "known_unknowns": ["recovery_timeline"], "assumptions": ["demand_stable"] }
  },
  "provenance": { "derived_from": [], "method": "observation", "signature": null },
  "action_hints": { "priority": "high", "ttl_seconds": 3600, "requires_ack": true },
  "epistemic": { "value": 1.0, "weight": 0.0, "evidence_count": 0, "last_tested": null }
}
```

A `task_result` capsule also carries `"outcome": {"status": "success" | "failure", "detail": "…"}`
and must derive from the task it reports on. No other capsule may carry an outcome.

On the wire it travels inside an envelope: `{capsule, sig, alg: "ed25519", pubkey_id}`.

Vocabulary (`vocab.json`, `sc.schema.json`):

- **Intents:** inform, request, query, confirm, refuse, ack
- **Triggers:** none, task, task_result, threshold, stuck, heartbeat, announce
- **Claim types:** observation, inference, assumption, directive
- **Predicates:** affects, causes, depends_on, contradicts, supports
- **Provenance methods:** synthesis, merge, relay, observation, reply, rule

## How it fits together

```text
sender                                   receiver
──────                                   ────────
Capsule ─to_wire─► dict ─sign─► envelope ──TCP──► Peer.recv
         (Peer.send also stores it signed)          │  signer label = from?  addressed to me?
                                                    │  session bound to this peer?  hello first?
                                                    │  key pinned, or first-contact pin after verify
                                                    │  verify signature
                                                    │  ingest: schema, versions, clock, vocab, coherence
                                                    │  expired?  already in ledger (replay)?
                                                    ▼
                                            store signed in ledger
                                                    │
                                            Scheduler.dispatch ─► route by trigger / agent
                                                    │           task_result: outcome ─► rule trust
                                                    ├─► RuleEngine.evaluate ─► rule outputs
                                                    │
                                            replies + rule outputs ─► Peer.send to that agent's session
```

| File | Role |
| --- | --- |
| `primitive.py` | Frozen dataclasses: `Capsule`, `Claim`, `Relation`, `Provenance`, enums |
| `validator.py` | JSON Schema + version, clock skew (30s future, 7d past), vocab, coherence, staleness checks |
| `interpreter.py` | `to_wire` / `from_wire`, `ingest`, `merge`, `actionable`, `is_expired`, `render` |
| `envelope.py` | Canonical JSON, SHA-256 `digest`, Ed25519 `sign` / `verify` |
| `store.py` | SQLite ledger keyed by digest, indexed sender/receiver/topic/expiry, `find()` on those columns; `python -m store <db>` dumps it, `python -m store import <jsonl> <db>` migrates old ledgers |
| `self_describe.py` | Reads the code, docs and test results into signed `self.*` capsules in `store/self.db` |
| `peer.py` | `Node` (key, pins, ledger, lock) and `Peer` (one session): trust-on-first-use pins, verify, validate, replay check, store signed |
| `scheduler.py` | Kernel: stores in and out, routes by trigger, fires rules, turns verified task outcomes into opinions |
| `rules/` | `engine.py` (issue, load, fire, learn, maintain), `pattern.py` (`when` matching), `builtin.json` (starter rules), `python -m rules` |
| `weight.py` | Leighton Weight: exponential decay, `Opinion` (value + weight), `blend` |
| `handshake.py` | Hello capsule, version and predicate negotiation, `clock_offset` |
| `run_alice.py`, `run_bob.py` | Multi-peer server; client that runs as any `--name` |
| `run_gateway.py`, `edge/gateway.py` | Edge gateway: device frames on serial or stdio ↔ one signed session per device |
| `firmware/m5stickc_plus2/` | MicroPython firmware for the M5StickC PLUS2: `main.py`, `sctalk.py` (protocol), `st7789.py` (screen), `sensors.py`, and `deploy.py` |
| `demo.py` | Single-process walkthrough of the whole pipeline |
| `boot/genesis.py` | A node's first capsule |
| `boot/discovery.py` | `peers.json` (`host:port` entries), `parse_peer` |
| `edge/upgrade.py`, `edge/sc_edge.json` | Stripped ESP-NOW wire form (≤16/32/120-char fields) and conversion |
| `edge/sensors.py`, `edge/sc_sensors.json` | A device's sensor list, checked, into a `__sensors__` capsule |
| `agents/` | `EchoAgent`, `RelayAgent` |
| `hal/` | `FileTransport`, `SocketTransport`, `SocketListener` (many peers), `local_addresses`, clock, storage re-export |
| `leighton_weight_readme.py` | The decay theory, draft |
| `NOTES.md` | Design decisions, open questions, what's next |

### Leighton Weight in one paragraph

`W(t) = W0 · e^(−k·t)`, with `k = k0 / (1 + evidence) / stakes`. Each agent keeps a
private `Opinion` per topic, saved in its database: `value` on [−2, +2] where **+1 means unknown**, and `weight`
(conviction) ≥ 0. A success adds +0.1 value and +1 weight; a failure −0.2 and +3.
Over idle time weight decays and value slides back toward +1 at the same rate.
**Receiving a capsule is not evidence.** Only `Scheduler.record_outcome()` moves an opinion.

**Opinions travel as hints; belief is earned locally.** `blend(own, peer, trust=0.1)` gives
a decision-time view: the peer's weight counts at `trust` of its face value, and the view keeps
your own evidence count and decay clock. Never store it as your opinion.

## The good, the bad, and the ugly

### The good — works, and tests or real runs show it

- **Trust boundary.** Ed25519 over canonical JSON (sorted keys, no whitespace).
  A signature verifies with the right key and fails with the wrong one.
- **Exact, indexed store.** One SQLite file per node. `store.get(digest)` returns exactly what
  was hashed, by index: 0.06 ms at 20,000 capsules, where the old JSON Lines file took 48 ms
  to scan. Duplicate appends are no-ops. Capsules stay plain JSON, so `sqlite3` and
  `json_extract` can query them. Several processes can safely open one store.
- **Two-way, signed ledger.** Capsules sent and received are both stored with their signatures,
  so `store.envelope_of(digest)` verifies from the ledger alone. Scheduler replies carry
  `derived_from` pointing at their parent.
- **Real validation.** Schema, version, clock skew, unknown predicates, incoherent
  intents (an `inform` with no claims), expired claims, merges with fewer than 2 parents.
  Edge messages are checked against `sc_edge.json` on upgrade and downgrade.
- **Several nodes talk at once.** Alice serves concurrent sessions over TCP. Each peer pins
  keys, exchanges a signed capsule and a signed ACK, and gets its own reply. Both ends compute
  identical digests for the same capsules.
- **Rejections are tested.** `Peer.recv` rejects, and `tests/test_peer.py` proves:
  - capsules before a hello, even from a pinned agent
  - a hello whose signature doesn't match its offered key (it never gets pinned)
  - tampered content
  - a new key for a pinned agent
  - a signer label that differs from `from`
  - capsules addressed to someone else
  - another agent's capsule on a session bound to a different peer
  - replays, including a replayed hello and a replay after a restart
  - expired capsules
- **Thread-safe core.** Store, pins and socket sends are locked. Tests hammer a shared store,
  two connections to one database, and a shared node from 8 threads, and check every record.
- **Merge.** Same-statement claims keep the higher confidence. Relations and unknowns
  combine. Trigger survives.
- **Honest epistemics.** Receipt leaves an opinion at unknown; an observed outcome moves it;
  idle time returns it to unknown.
- **Edge round-trip.** A stripped ESP32 message upgrades to a full capsule and
  downgrades back to the identical stripped form.
- **Edge devices learn with the rest.** Through the gateway, a device's threshold report fires
  Alice's rule, the task reaches the device, and its `task_result` moves the rule's trust, signed
  as the device's agent. Tested with a simulated device, and run on a real M5StickC PLUS2.
- **Rules learn from what happens.** Rules are signed capsules; only a node's own rules run.
  Their outputs cite the rule and the input. Reported outcomes move each rule's trust, a rule with
  no trust left stops firing, and a rule whose trust fades is forgotten. Tested across processes
  and from the attacker's side: forged and tampered rules, outcomes from the wrong agent,
  duplicate reports.
- **The system knows what it's made of.** `self_describe.py` turns every Python file, test,
  design decision, open question and known limit into signed capsules that pass the same
  validation as any other. Test results come from actually running the tests. Because ids follow
  content, a rerun stores only what changed, and each file's versions chain through `derived_from`.
- **Small dependency surface.** `jsonschema` and `cryptography`, plus Python's own `sqlite3`.

### The bad — known limits, by design for now

- **Star topology.** Clients, the phone and the gateway all talk only to Alice.
  There's no relaying between Bob and Carol, and a reply for an agent with no open session is
  stored but not delivered (no queue). Clients don't reconnect mid-session.
- **One lock for dispatch.** Alice runs the scheduler under a single node lock, so capsules are
  processed one at a time. One thread per connection is fine for a handful of peers, not hundreds.
- **Trust on first use is only as good as first contact.** Whoever says hello first as
  `agent://bob` gets pinned as Bob. There's no registry or root of trust, and no way to rotate a key.
  Genesis doesn't create or announce a key; `peer.py` does.
- **Replay protection depends on the ledger.** A capsule is a replay if its digest is already
  stored. Pruning only drops capsules past their TTL, and expired capsules are rejected anyway, so
  the window stays closed while the ledger is intact. Delete or edit a ledger and replays inside
  their TTL get through.
- **Storage grows until pruned.** SQLite makes lookups fast, not files small: at 20,000 capsules
  the database is about 25 MB, roughly 1.3× the old JSON Lines file, because of its indexes.
  `Store.prune_expired()` deletes expired rows and gives the space back, but nothing calls it on
  a schedule yet. Pins are still a JSON file per node, which two processes must not share.
- **Signing lives outside the kernel.** `Peer` signs and stores signed records. `demo.py` and
  `Scheduler` used alone still store unsigned replies.
- **An outcome is the worker's word.** A rule learns from what the agent asked to do the task
  reports. Alice checks that the report comes from that agent and counts it once, but can't check
  that it's true: an agent that always reports success makes a bad rule look good.
- **Rules don't chain.** A rule never fires on another rule's output. That prevents loops, but
  multi-step reasoning needs a person or agent in between.
- **Hints aren't wired in.** Nothing fills a capsule's `epistemic` block from the sender's
  opinion, and the scheduler never calls `blend`.
- **Stubs.** `_escalate` and `_handle_threshold` just ACK (rules can act on those capsules instead).
  `RelayAgent` and `hal/clock.py` are unused.
- **Two machines, one network, by hand.** PC ↔ phone was checked by hand on a single hotspot:
  no NAT, no routing between networks, no lossy links. No automated test spans two machines, and
  the run scripts and `EchoAgent` have no asserting tests beyond the network checks.
- **The self-description stays home and must be refreshed.** `self.*` capsules live only in
  `store/self.db`; no node sends them to peers. They expire after 7 days, and nothing reruns
  `self_describe.py` automatically.
- **Tunables with no definition yet.** `stakes_factor` means nothing concrete. The stance bands are
  lopsided: from unknown, 2 successes reach `leaning_trusted` but 1 failure reaches `unclear`.

### The ugly — will bite you without warning

- **Logged values are rounded, stances aren't.** Alice decays an opinion before each outcome, so a
  value can sit just under a stance boundary and still print as that boundary: the rule once showed
  `value=+1.50 stance=leaning_trusted`, where `trusted` starts at exactly 1.5.
- **`SocketTransport.recv` on its own trusts the sender's label.** It returns the envelope's
  self-declared `pubkey_id`. `Peer.recv` does the checking; call the transport directly
  and you get none of it.
- **Some provenance is missing.** Agent replies (`EchoAgent`) don't set `derived_from`, nor do
  edge messages that answer nothing. `Provenance.signature` is always `null` and unrelated to the envelope signature.
- **No merged capsule is valid.** `merge` joins senders into `agent://alice+agent://bob` (even
  `agent://bob+agent://bob` for one sender), which the schema rejects. Merge works only on capsules
  you never validate.
- **Self-description parses doc headings by name.** Decisions, open questions, next steps and
  limits are read from `## Decisions`, `## Open questions`, `## Next` in `NOTES.md` and
  `### The bad` / `### The ugly` in this README. Rename a heading and that capsule silently
  disappears, or `self.open_questions` records "0 open questions".
- **Arrows crash on some Windows consoles.** `render()` prints `•` and `→`. When stdout is cp1252,
  as when Git Bash pipes Python's output, `demo.py` stops with `UnicodeEncodeError`.
  PowerShell and file redirection work; `self_describe.py` forces UTF-8 itself.
  Workaround: `PYTHONIOENCODING=utf-8`.
- **The gateway forgets on restart.** Short task ids and seen message ids live in memory. Restart
  the gateway and a result for an earlier task is refused (`edge_unknown_task`), and a device message
  id it saw before is accepted again. The gateway also holds every device's key: whoever controls
  it can speak as any of its devices.
- **An edge outcome is a button press.** The stick reports what a person pressed; nothing checks
  that the tilt was real. The stick's link is USB serial, not ESP-NOW.
- **Small device buffers.** The stick's serial input buffer is small. The gateway paces frames and
  the firmware reads between screen rows (10 frames sent back to back with no gap all arrived), but
  a long enough burst could still overflow it; a lost task simply never gets an outcome.
- **Alice knows the sensors, not their readings.** The stick's `__sensors__` capsule reaches Alice,
  but readings are only displayed, so no `sensor:<id>` opinion exists yet. The margins in it are the
  firmware's defaults, not an operator's (`__setup__` isn't built).
- **Trust lives only in the node's database.** Delete `store/<name>.db` and every rule and topic
  opinion starts over at unknown; there's no backup or export of opinions.

## Roadmap

1. ~~Fix `SocketTransport` framing.~~ Done.
2. ~~Two-process test.~~ ~~Three nodes.~~ ~~Two machines~~ (PC ↔ phone, see
   [Two machines](#two-machines)). Next: networks with NAT, and reconnecting clients.
3. Identity binding: ~~trust on first use~~ done; key registry or root of trust, key rotation.
4. ~~Replay protection.~~ Done.
5. ~~SQLite ledger.~~ Done. Next: prune on a schedule.
6. ~~Outcome field on `task_result` capsules.~~ Done, with rules that learn from it.
   ~~Persisting topic opinions.~~ Done. ~~Edge devices reporting outcomes.~~ Done, through the
   gateway, verified on an M5StickC PLUS2 over USB serial. Next: ESP-NOW between two boards.
7. Relaying between peers and a queue for undelivered replies.
8. ~~Asserting tests for validator, interpreter, merge and scheduler.~~ Done.
9. ~~Self-description capsules.~~ Done. Next: share them with peers after the hello, and decide
   whether passing test results count as outcomes.

See [CHANGELOG.md](CHANGELOG.md) for history and [NOTES.md](NOTES.md) for design decisions and open questions.
