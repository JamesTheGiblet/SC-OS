# Changelog

Each release lists what changed, then an honest verdict: the good, the bad, and the ugly.

## [Unreleased]

Since v0.1: several nodes over TCP, identity, replay protection, a SQLite ledger,
self-description capsules, rules that learn from outcomes, persisted topic opinions, asserting
kernel tests, a first run across two machines (PC ↔ phone), and fixes for bugs that only showed
up across a process boundary.

### Added

- **Target design written down.** `NOTES.md` has the thesis (a seed, not a blueprint) and the
  target design (kernel, law, axis, sharing, hardware/sensor/setup capsules, read path, bootstrap,
  edge, growth, build order), each marked built, partial or not built.
- **Two-machine readiness.** `run_alice.py --host 0.0.0.0` prints the addresses other machines can
  use (`hal.transport.local_addresses`). `run_bob.py` without `--host` connects to the first
  `host:port` in `peers.json` (`boot.discovery.parse_peer`), and says what to check when it can't
  connect instead of printing a traceback. Both sides warn when the peer's clock is more than 5 s
  off (`handshake.clock_offset`). Keys, pins, ledgers and the default rule file are found next to
  the scripts, not in the working directory. Verified: Alice on `0.0.0.0`, Bob and Carol started
  from another directory, connecting over the machine's network address; both matched their pins,
  and the rule and topic opinion learned from their outcomes.
- **Verified across two machines** (2026-09-17): Alice on a Windows PC, `agent://phone` on an
  Android phone in Termux, over Wi-Fi. Covered: first-contact pinning, pin matches on restart,
  replay rejection, success and failure outcomes, client killed mid-session, silent network drop
  (Alice timed out after 30 s and kept serving), overlapping sessions from the phone and the PC,
  and trust surviving an Alice restart. The phone's clock was 1.0 s behind the PC's, logged as
  `-1.0 s` by Alice and `+1.0 s` by the phone. `verify-high-confidence-risk` became the first
  rule to reach `trusted` (+1.70, weight 21.97, 16 outcomes from three agents on two machines).
- Both run scripts log the peer's clock offset on every hello. `run_bob.py` exits quietly on Ctrl+C.
- **Edge devices report outcomes, through a gateway.** The stripped format gains `re` (the short id
  of the message answered), `o` (`success` | `failure`) and `od` (detail); `o` and `re` are required
  on `task_result` and `o`/`od` are refused elsewhere. `from_edge_wire(..., derived_from=)` sets the
  outcome and parent; `to_edge_wire(..., re=)` carries them down.
  `edge/gateway.py` and `run_gateway.py` (`--serial PORT` or `--stdio`) bridge line-delimited
  device frames to Alice: each device is its own agent (`agent://<src>`) with a key the gateway
  holds and its own signed session, so tasks reach it and its outcomes count. The gateway maps
  short ids to full ones, rejects repeated device message ids, unknown or expired tasks, bad
  names and messages for anyone but Alice, and reopens sessions Alice closed while idle.
  `tests/test_gateway.py` (9): a simulated device's outcome moves Alice's rule over real TCP,
  including after a dropped session and through the script on stdin/stdout. 136 tests in all.
- **Firmware for an M5StickC PLUS2** (`firmware/m5stickc_plus2/`, MicroPython). Tilt past 40°
  reports `tilt_risk`; the task from Alice's rule blinks the LED; button A reports success, B
  failure. `sctalk.py` is the protocol with no hardware imports, tested on the PC against the
  gateway and a real Alice. `deploy.py` copies files over the raw REPL with DTR/RTS held low
  (`mpremote` resets this board when it opens the port).
- **Screen and sensors on the M5StickC PLUS2.** `st7789.py`: a small ST7789 driver (landscape
  240×135) that pushes only changed rows, with cached large-text glyphs. `sensors.py`: MPU6886
  accelerometer, gyroscope and die temperature; BM8563 clock; battery voltage; buttons A, B, C.
  The screen shows clock, tilt, acceleration, rotation, IMU temperature, battery, link state, the
  waiting task with its A/B prompt, and the last outcome. Button C toggles the backlight.
  `deploy.py` copies the new files and sets the clock from the PC; it reports a busy port instead
  of a traceback. Checked on the device: flat stick read 1.01 g and 2.5° tilt, battery 4.16 V on
  USB, clock correct; 10 frames sent back to back with no gap all arrived while the screen redrew.
  Found on the device and fixed: 40 MHz SPI on the screen's pins crashed the firmware in a boot
  loop (the limit on those pins is 26.7 MHz; it now uses 20 MHz).
- **Time-of-flight distance sensor on the M5.** A VL53L0X (CJMCU V2 board) on the Grove port (SDA
  32, SCL 33). `firmware/m5stickc_plus2/vl53l0x.py` ports Pololu's initialisation (SPAD setup, tuning
  table, timing budget, VHV and phase calibration) with continuous, non-blocking reads. The stick
  detects it at boot and only then adds `tof` (0–2000 mm, margins 50–1200 mm) to its description;
  distance is sent as a reading when something is in range and shown on a new screen row. Setup
  overrides for sensors not present are ignored instead of resetting the rest. Alice: sensors with
  no dedicated check (`tof`) earn trust from staying inside their physical range. Verified: the
  stick described 9 sensors, readings of 144 and 122 mm reached Alice, and `sensor:m5-96c048/tof`
  formed; distances followed a hand on the screen. First readings were all 8190 (status 6, weak
  signal) with nothing close enough in front. 163 tests.
- **Operator setup** (target design 8). `provision.py issue <file>` checks a setup (margins and
  `sample_ms` per sensor) against the device's `__sensors__` description and stores it as a signed
  `__setup__` task from Alice; `provision.py show` lists each device's latest setup as applied or
  waiting. `SetupKeeper`, a new observer on Alice, sends a relay copy whenever the device's readings or
  description show the setup isn't applied, at most once a minute. `Scheduler` observers may now
  return replies (`respond`). The gateway turns a `__setup__` task into a setup frame; the M5 applies
  all of it or none (`sctalk.apply_setup`), saves `setup.json`, reports a `task_result`, and describes
  itself again. The M5's tilt threshold is now tilt's `margin_high`. Found by the new test and fixed
  before release: the gateway turned Alice's ACKs on topic `__setup__` into setup frames too.
  Tests: `tests/test_provision.py` (9). Verified on the stick: a setup (tilt 30°, battery 3.5–4.3 V)
  issued while it ran was delivered on its next reading, applied, confirmed by its description, and
  still in force after a reboot ("tilt past 30 degrees to report"). 161 tests.
- **Sensors earn trust from plausibility checks** (target design 9, the read path). The M5 sends
  readings (accel, gyro, tilt, imu and chip temperature, battery, UTC clock) when one moves past its
  deadband, at most every 5 s and every 25 s regardless. The gateway builds a `__readings__` capsule
  (`edge/sc_readings.json`, trigger `heartbeat`, so Alice doesn't reply). `sensing.py`: Alice's
  `SensorObserver` checks each one herself (physical range from `__sensors__`, 1 g at rest, gyro near
  zero while steady, tilt agrees with the accelerometer, temperature rate, battery range and jumps,
  clock within 120 s) and records outcomes for `sensor:<device>/<id>`, at most one per sensor per
  minute. `Scheduler` gains `observers`, `record_observation(key)` and `opinion_of(key)`; topic
  outcomes use the same path. `run_alice.py` prints sensor trust at shutdown and now prunes expired
  capsules hourly. The M5's clock is kept in UTC, with the local offset in `tz.txt` for the screen.
  Tests: `tests/test_sensing.py` (13). Verified on the stick: Alice judged all seven readable sensors
  plausible (accel 1.02 g at rest, gyro 1.6 dps, clock 1 s off) and formed their opinions. 152 tests.
- **Edge devices describe their sensors** (target design 7). A device sends
  `{"src", "sensors": [...], "absent": [...]}`; the gateway checks it against `edge/sc_sensors.json`
  (all ten fields, unique ids, `min ≤ margin_low ≤ margin_high ≤ max`) and builds a `__sensors__`
  capsule (`edge/sensors.py`): trigger `announce`, one claim `sensor:<id>` per sensor with
  `key=value` evidence for each field, and `absent` hardware as known unknowns. Signed as the
  device's agent. The gateway sends `{"dst": "*", "cmd": "describe"}` when it opens a serial port,
  so a device that booted earlier describes itself. The M5 firmware declares eight sensors in
  `sensors.py` (accel, gyro, tilt, imu_temp, chip_temp, battery, clock, buttons) and sends them at
  boot and on request. Tests (3): list checks, capsule shape, and the firmware's own list through
  the gateway into Alice's signed ledger. Verified on the stick: Alice stored
  `__sensors__` from `agent://m5-96c048` with all eight claims and both known unknowns. 139 tests.
- **Buzzer and chip temperature on the M5StickC PLUS2.** Non-blocking tones on pin 2: two beeps
  when a task arrives, a chirp on success, a low tone on failure (heard on the device). The screen
  adds the ESP32's internal temperature, which has a large fixed offset. The microphone isn't
  read: it answers when clocked, but MicroPython's I2S has no PDM input and counting its data edges
  with the pulse counter didn't track loudness. IR (pin 19, shared with the LED) sends no codes.
- **Verified on the device** (2026-09-17): `agent://m5-96c048` through `run_gateway.py --serial COM4`.
  Its outcomes moved topic `tilt_risk` and the verify rule, up on A and down on B, as the model says.
  Found on the device and fixed: frames ignored after the port was reopened (both sides now parse
  from the first `{`); an ACK and task back to back were lost, most likely overflowing the device's small input buffer
  (the firmware waits on input instead of sleeping; the gateway leaves 50 ms between frames and
  skips `# ` note lines); new tilt reports replaced a waiting task (one question at a time).
- **Asserting tests for the kernel and the law.** `tests/test_weight.py` (14, was a print script),
  `tests/test_validator.py` (13), `tests/test_interpreter.py` (13, including merge),
  `tests/test_scheduler.py` (14), `tests/test_network.py` (6). 126 tests in all.
- **Rules as capsules that learn** (`rules/`). A rule is a signed capsule on topic `rule.<name>`
  whose directive claim holds a JSON spec: a `when` pattern (topic, from/to globs, intent, trigger,
  claim type, minimum confidence, predicate) and capsules to emit `then`, with placeholders.
  - **Firing:** outputs carry `provenance.method = "rule"` and `derived_from = [rule, input]`.
    A rule fires at most once per input, never on its own node's capsules or another rule's output
    (no loops), and not once its value is 0 or below.
  - **Own rules only:** a node runs only rules it signed; peers' rules are stored, not executed.
    Tampered or forged rules fail the signature check and are skipped. `adopt()` re-issues a
    peer's rule as the node's own, starting at unknown.
  - **Learning:** a `task_result` outcome for a rule's output moves the rule's opinion, if it
    answers a task this node sent, comes from the agent it was sent to, and wasn't counted before.
  - **Lifetime tied to Leighton Weight:** `maintain()` re-issues live rules before their 7-day
    TTL ends. Tested rules are forgotten when decayed weight falls below 0.05, untested ones after
    30 days. Forgotten rules stay forgotten unless issued with `--force`. Editing a rule makes a new
    rule that derives from the old one and starts at unknown.
  - `rules/builtin.json`: verify-high-confidence-risk, cool-hot-sensor, escalate-stuck,
    question-contradiction. `python -m rules list|issue|maintain --node <name>`.
  - `run_alice.py` issues the built-in rules at start and maintains them hourly; `run_bob.py`
    carries out a rule's task and reports `--outcome success|failure|ignore`.
  - Verified across processes: Bob success (+1.10, weight 1), Carol failure (+0.90, weight 4),
    Bob success (+1.00, weight 5); an ignored task changed nothing; trust persisted across restarts.
- **Outcome field.** `task_result` capsules carry `outcome: {status: success|failure, detail}` and
  must derive from the task they report on; no other capsule may carry an outcome. The field is
  omitted when absent, so existing capsules keep their digests. `Outcome` in `primitive.py`.
- **Local belief in the node database:** `opinions` table (`Store.get_opinion`, `put_opinion`,
  `opinions`) and `counted_outcomes` (`Store.mark_outcome_counted`). `Store.find(topic_prefix=)`.
- **Multi-node sessions.** `run_alice.py` serves concurrent peers over TCP, one thread per
  connection, sharing one node and one scheduler, and routes replies by `to` to that agent's
  open session. `--sessions N` exits after N sessions. `run_bob.py` connects as any
  `--name`, with `--hold` to overlap sessions and `--replay` to test replay rejection.
- **`hal.transport.SocketListener`** accepts many connections, each its own `SocketTransport`.
  `SocketTransport.close()` added; `send` is locked so concurrent replies can't interleave.
- **`peer.py`: signed sessions.** `Node` holds one agent's key, pins, ledger and lock; `Peer` is
  one session over one transport (`Node(...).session(transport)`).
  - **Trust on first use.** A hello carries the sender's Ed25519 key and is signed by it. The first
    hello that verifies and validates pins agent id → key in `store/<name>.pins.json`.
    Keys persist in `keys/<name>.ed25519`.
  - **Session binding.** A session must open with a hello, even from a pinned agent, then
    carries only that agent's capsules. `Peer.send` refuses to send as another agent or to
    another receiver.
  - **Replay protection.** A verified capsule whose digest is already in the ledger is rejected,
    across restarts too. Expired capsules are rejected.
  - `Peer.recv` also rejects: a changed key for a pinned agent, a bad signature, a signer label
    that isn't the capsule's `from`, a capsule addressed to someone else, and a capsule that
    fails validation.
  - `Peer.last_pin` is `"new"` or `"known"`; run logs say `first contact, key pinned` or
    `key matches pin`.
- **Signatures in the ledger.** `Store.append(capsule, envelope=wire)` keeps the signature;
  `Store.get_record(digest)` returns the full record and `Store.envelope_of(digest)` an envelope
  ready for `verify`. An envelope wrapping a different capsule raises `ValueError`. Adding a
  signature to a capsule stored unsigned updates it; a signed record is never replaced.
  `python -m store` shows `signed:<pubkey_id>` or `unsigned`.
- **SC-OS describes itself in capsules.** `self_describe.py` reads every Python file (with `ast`),
  the tests, `vocab.json`, the schema, `NOTES.md` and `README.md`, runs the test files, and stores
  signed capsules from `agent://sc-os` in `store/self.db`. Topics: `self.identity`, `self.module`
  (per file, with `depends_on` relations), `self.tests` (with `supports` relations),
  `self.test_results`, `self.vocabulary`, `self.decisions` (directive claims),
  `self.open_questions` (known unknowns), `self.limits`, `self.next`. Every capsule passes
  `ingest()`. Ids come from content, so an unchanged rerun stores nothing, and a changed file's new
  capsule has `derived_from` pointing at the previous version. `--show [TOPIC]` prints the current
  description, `--dry-run` previews, `--no-tests` skips running tests without erasing old results.
  First run: 43 capsules, all tests PASSED.
- **`Store.find(topic=, sender=, receiver=, capsule_id=, unexpired_at=)`** queries the indexed
  columns.
- **`python -m store import <jsonl> <db>`** migrates pre-SQLite ledgers, keeping stored times and
  signatures, so migrated history still blocks replays.
- **Tests with asserts.** `tests/test_store.py` (15), `tests/test_transport.py` (9, real localhost
  TCP), `tests/test_peer.py` (17), `tests/test_rules.py` (19), `tests/test_self_describe.py` (6).
  `tests/test_weight.py` asserts the sharing rule.
- `README.md` (with a "why capsules" section), this changelog, and rewritten `NOTES.md`
  recording design decisions and open questions.

### Changed

- **Topic opinions persist.** `Scheduler.opinions` (an in-memory dict) became `Scheduler.opinion(topic)`
  and `Scheduler.opinions()`, backed by the store's opinions table under `topic:<topic>`. Each outcome
  decays the stored opinion to now, then observes; reads decay to now. Verified across processes:
  the opinion from a run was read back by a new process.
- **The scheduler learns only from verified outcomes.** `_handle_task_result` finds the task a
  `task_result` answers; if this node didn't send it to the reporting agent, it replies REFUSE
  `unknown_task`. Otherwise the outcome, counted once per task, updates the topic opinion and the
  rule that asked for the task. `Scheduler(..., rules=RuleEngine)` fires rules on every dispatched
  capsule; `Scheduler.learned` lists what each outcome changed.
- **Schema:** provenance method `rule` (needs ≥ 2 parents); `outcome` object; coherence checks for
  `task_result`. Store schema version 2 (new tables are created in existing databases).
- **Capsule storage moved to SQLite.** Same `Store` API, one database file per node
  (`store/<name>.db`, WAL mode). Each capsule is a row: JSON body, unique digest, stored time,
  signature columns, and indexed `capsule_id`, `sender`, `receiver`, `topic`, `intent`,
  `expires_at`.
  - `prune_expired` deletes rows and runs incremental vacuum, so the file shrinks.
  - Several processes can open the same store.
  - Measured at 20,000 signed capsules, JSON Lines → SQLite: lookup 48 ms → 0.06 ms,
    open 351 ms → 7 ms, append 1,646/s → 1,462/s, prune of half 941 ms → 976 ms,
    disk 19.7 MB → 25.3 MB (14.2 MB after pruning half).
  - Callers use `.db` paths; `store/*.db*`, `store/*.json` and `keys/` are git-ignored.
- **Sharing rule decided: opinions travel as hints; belief is earned locally.**
  `blend(a, b, alpha)` became `blend(own, peer, trust=0.1)`. Before, it took on the peer's
  whole weight and evidence count, so two tests of your own plus a 50-test peer came out
  `trusted` with n=52 and slow decay, and `alpha` was never used. Now the peer's weight counts
  at `trust` (0..1), and the result keeps your own `evidence_count` and `last_tested`.
  The same example gives value +1.40, `leaning_trusted`, n=2.
- Run-script timings say what they measure: Bob logs a full `cycle` (sign, send, Alice's verify,
  dispatch and sign, receive, verify); Alice logs time since the sender created the capsule.
  Neither is network latency.
- `run_bob.py` reports `FAIL agent://alice closed the connection` when dropped, instead of a
  traceback.

### Fixed

- **Decay was counted twice when an opinion was ticked more than once.** `Opinion.tick` measured
  elapsed time from `last_tested` every call but applied it to already-decayed weight, so ticking
  at day 7 then day 30 gave weight 1.89 where one tick at day 30 gives 2.07. The old weight test
  ticked exactly that way, so its printed trajectories were wrong. `Opinion` now records
  `decayed_to`; stepwise and single ticks agree. Stored rule opinions were unaffected (each load
  ticks once).
- **A forged hello could poison a pin.** `peer.py` pinned the key offered in a hello before
  checking the hello's signature. A hello for `agent://bob` offering any key, signed by anyone,
  was rejected but left that key pinned on disk, locking the real Bob out. Pins are now written
  only after the signature verifies against the offered key and the capsule validates.
  Confirmed against the previous code before fixing.
- **Every reply failed validation.** Scheduler replies set `provenance.method="reply"`, which
  `sc.schema.json` didn't allow. The single-process demo never validated a reply; the first
  two-process run rejected Alice's ACK. `reply` is now an allowed method.
- **Socket framing lost messages.** `SocketTransport.recv` kept no buffer between calls:
  two messages in one TCP read raised `JSONDecodeError: Extra data`, and bytes after the
  first newline were dropped. Leftover bytes are now kept for the next `recv`.
  Frames are capped at 1 MiB. A listener whose peer disconnects accepts the next connection.
- **Handshake vocab version.** `make_hello` sent Python's set repr (`vocab_version={'1.0'}`).
  It now sends `vocab_versions=1.0`. `negotiate` compares vocab versions, raises
  `no shared vocab version` on mismatch, and returns `vocab_version`.
- **Version choice compared strings.** `10.0` now beats `9.0`.
- **Edge downgrade emitted invalid messages.** `sc_edge.json` now allows `tr: "none"`
  (as `vocab.json` does). `from_edge_wire` and `to_edge_wire` validate against the
  schema and raise `CapsuleRejected("edge_schema", …)` on a bad message.
- **Pruning rewrote history.** The JSON Lines store reset `stored_at` on every record it kept
  after a prune. Fixed there, and SQLite pruning deletes rows without touching the rest.
- **Pruning didn't free disk space** in the first SQLite version: `PRAGMA incremental_vacuum`
  frees one page per result row and only one row was read. All rows are now read; a test checks
  the file shrinks.
- **Leighton Weight draft contradicted the code.** `leighton_weight_readme.py` said value
  does not decay. It now gives the rule `weight.py` implements, the step sizes, and the
  sharing rule, and says only observed outcomes reinforce.

### The good

- Three real processes run overlapping sessions; each peer gets its own verified ACK, and a
  byte-identical replay is rejected, including one from before the storage migration.
- The trust boundary is tested from the attacker's side: forged hellos, tampering, key changes,
  label mismatches, misaddressing, cross-session capsules, replays and expiry.
- The ledger proves who said what on its own, and lookups stay fast as it grows.
- SC-OS describes its own code, tests, decisions and limits in its own format, generated from the
  source so the description can't drift, with every test file passing when last described.
- Rules are behavior as signed capsules: their outputs explain themselves, and their trust moves
  only on outcomes reported by the agent that did the work, counted once.
- The same code ran unchanged on Windows and on Android (Termux): signed sessions, pinning, replay
  rejection, learning, dropped connections and overlapping sessions all behaved as on one machine.
- The kernel and the decay law have asserting tests, and the first of them found a real bug
  (decay counted twice on repeated ticks).

### The bad

- Still a star, now across two machines: no relaying, no queue for offline agents.
- Trust on first use trusts whoever arrives first; no registry, no key rotation.
- SQLite files are ~1.3× the old JSON Lines size; only Alice prunes, hourly.
- Two machines checked by hand on one Wi-Fi hotspot only: no NAT, no lossy links, no automated test.
- The self-description isn't shared with peers, expires after 7 days, and nothing reruns it.
- A reported outcome is the worker's word; nothing checks it's true.
- Rules don't chain.

### The ugly

- `SocketTransport.recv` used directly still trusts the sender's self-declared label;
  only `Peer.recv` checks it.
- `merge` builds an unaddressable sender (`agent://alice+agent://bob`), so no merged capsule
  passes validation.
- Agent replies and edge messages that answer nothing don't set `derived_from`;
  `Provenance.signature` is always null.
- Self-description finds decisions, questions and limits by heading name; rename a heading and
  that capsule silently vanishes or reports zero items.
- `demo.py` crashes with `UnicodeEncodeError` on `→` when stdout is cp1252 (Git Bash pipes on
  Windows). PowerShell and file redirection are fine.
- The gateway keeps short task ids and seen device ids in memory, and holds every device's key.
  A restart loses in-flight tasks and reopens the duplicate window; control of the gateway is
  control of its devices.
- An edge outcome is a button press; nothing checks the tilt was real. The M5 link is USB serial,
  not ESP-NOW, and the device's small input buffer can still lose a task in a burst.
- Plausibility checks catch impossible readings, not a sensor that is steadily wrong; thresholds
  are tuned for the M5.
- The ToF sensor earns trust only from staying in range, and its accuracy was checked by hand.
- Setups are signed by the node's key (no separate operator identity), accumulate on the device,
  and reach it only while it reports.
- A still M5 adds about 3,500 readings capsules a day; its sensor description expires after a day,
  and range checks stop until it describes itself again.
- Trust exists only in the node's database; lose the file and every rule and topic opinion
  starts over.
- Logged values are rounded but stances aren't: a rule printed `value=+1.50 stance=leaning_trusted`
  because its value was just under the 1.5 boundary.

## [v0.1] — 2026-09-16

First tagged state. Single-process kernel; the interface is frozen until the
two-process socket test passes.

### Added

- `envelope.py`: canonical JSON, SHA-256 `digest`, Ed25519 `sign`, `open_envelope`,
  `verify`, and an `Envelope` type with `to_wire()`. The module was imported by the
  store and demo but didn't exist, so Python found an unrelated `envelope` email library instead.
- `Scheduler.record_outcome(topic, success)`: the only way an opinion changes.
- `python -m store <path> [--full]` to dump a store; `Store.records()` for full records.
- `NOTES.md`, `.gitignore`, git repository.

### Changed

- `Scheduler.dispatch` stores every reply as well as the incoming capsule.
  Routing moved unchanged into `_route`.
- Scheduler replies set `provenance.derived_from` to the parent id, `method="reply"`.
- `dispatch` no longer records a success for every capsule it receives.
- `merge` carries the trigger: the first parent's, or the second's if the first is `none`.
- `demo.py` moved to the project root, starts from an empty store, and shows an
  opinion before and after a recorded outcome.

### Fixed

- **Store returned the wrong capsule.** The index stored the count of distinct digests
  where it needed line numbers. Once a duplicate was appended, the two went out of step,
  and on the next run `store.get` read an older capsule (`retrieved matches: False`).
  The index now tracks physical lines, and duplicate appends are skipped.
- `edge/upgrate.py` → `edge/upgrade.py`, `hal/storgae.py` → `hal/storage.py`
  (imports were failing).
- `demo.py` put the wrong directory on `sys.path` after the move.

### The good

- The demo runs end to end, and repeated runs no longer disagree with each other.
- The store is exact and content-addressed, so `derived_from` chains can be trusted.
- Receiving a capsule and seeing an outcome are separate events, as the theory requires.

### The bad

- Still one process. Identity isn't bound to keys. Signatures aren't stored.
- `record_outcome` has no real caller; capsules can't say whether a task succeeded.
- No asserting tests; the demo trace is the test.

### The ugly

- Found while writing these docs, **not fixed in v0.1**:
  - `SocketTransport.recv` loses or garbles messages that share a TCP read.
  - `make_hello` sends a Python set repr as the vocab version.
  - `to_edge_wire` emits `tr: "none"`, which its own schema rejects.
  - `prune_expired` overwrites `stored_at` on every record it keeps.
- `leighton_weight_readme.py` contradicts `weight.py` on whether value decays.
