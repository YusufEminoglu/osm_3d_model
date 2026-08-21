/* Pins the walk-mode body model. Two of the three things asserted here were
 * actually wrong in the shipped viewer before v1.6.0 and none of them announced
 * itself:
 *
 *  - the walker accelerated to 9.2 m/s (33 km/h) and it just felt "fast";
 *  - friction was applied as (1 - k*dt), so the same key press covered a
 *    different distance at 30 fps and at 144 fps — invisible on the machine it
 *    was written on;
 *  - eye height was a magic 1.72 with no stated body behind it.
 *
 *     node tests/walk_check.mjs
 */
import assert from 'node:assert/strict';
import { WALK, eyeHeight, gaitOffset, gaitStrength, stepWalkVelocity, topSpeed } from '../web/src/walk.js';

const close = (a, b, tol, what) =>
  assert.ok(Math.abs(a - b) <= tol, `${what}: ${a} is not within ${tol} of ${b}`);

// ---------------------------------------------------------------- the body
// A 1.85 m person looks out from 1.73 m, not from the top of their head.
close(eyeHeight(1.85), 1.7316, 0.001, 'standing eye height at 1.85 m');
close(eyeHeight(1.85, true), 1.11, 0.001, 'crouched eye height at 1.85 m');
close(eyeHeight(1.60), 1.4976, 0.001, 'standing eye height at 1.60 m');
assert.ok(eyeHeight(1.85) < 1.85, 'the eyes must sit below the top of the head');
// A missing value falls back to the default body; a real but out-of-range one is
// clamped, so dragging the slider to an end stops there instead of snapping back.
assert.equal(eyeHeight(undefined), eyeHeight(WALK.defaultStature), 'no stature -> the default body');
assert.equal(eyeHeight(NaN), eyeHeight(WALK.defaultStature), 'a corrupt stored value -> the default body');
assert.equal(eyeHeight(0), eyeHeight(WALK.defaultStature), 'zero is not a body');
assert.equal(eyeHeight(0.5), eyeHeight(WALK.minStature), 'a tiny stature clamps to the minimum');
assert.equal(eyeHeight(99), eyeHeight(WALK.maxStature), 'an absurd stature clamps to the maximum');
assert.equal(eyeHeight(1.85), eyeHeight('1.85'), 'a slider value arrives as a string');

// ---------------------------------------------------------------- the gait
close(topSpeed(1), 1.4, 1e-9, 'walking pace');                       // 5.0 km/h
close(topSpeed(1, { sprinting: true }), 3.29, 0.01, 'jogging pace'); // 11.8 km/h
close(topSpeed(1, { crouching: true }), 0.7, 1e-9, 'crouched pace');
assert.ok(topSpeed(4, { sprinting: true }) < 14, 'even the fastest setting stays human-ish');
assert.equal(topSpeed('1'), topSpeed(1), 'a slider value arrives as a string');
assert.equal(topSpeed(undefined), topSpeed(1), 'no pace -> the default pace');
assert.equal(topSpeed(0), topSpeed(WALK.minPace), 'a zero pace clamps, it does not become normal speed');
assert.ok(topSpeed(1) < 2, 'walking pace must not creep back up to a vehicle speed');

// ------------------------------------------------- the velocity integrator
function run({ fps, seconds, holdFor = seconds, pace = 1 }) {
  const dt = 1 / fps;
  const top = topSpeed(pace);
  let v = { x: 0, z: 0 };
  let distance = 0;
  const steps = Math.round(seconds * fps);
  for (let i = 0; i < steps; i += 1) {
    const holding = i * dt < holdFor;
    v = stepWalkVelocity(v.x, v.z, 0, holding ? 1 : 0, top, dt);
    distance += Math.hypot(v.x, v.z) * dt;
  }
  return { speed: Math.hypot(v.x, v.z), distance };
}

// Holding forward settles at exactly the walking pace, from a standstill.
const held = run({ fps: 60, seconds: 3 });
close(held.speed, topSpeed(1), 1e-6, 'held speed converges to the walking pace');

// A person is at pace in a fraction of a second, not instantly and not in five.
const ramp = run({ fps: 60, seconds: 0.35 });
assert.ok(ramp.speed > topSpeed(1) * 0.85, `too sluggish off the mark: ${ramp.speed.toFixed(2)} m/s`);
const instant = run({ fps: 60, seconds: 0.03 });
assert.ok(instant.speed < topSpeed(1) * 0.5, 'the walker must accelerate, not teleport to pace');

// Frame-rate independence: the same three seconds of held input must cover the
// same ground on a 30 fps laptop and a 144 Hz desktop.
const distances = [30, 60, 90, 144, 240].map((fps) => run({ fps, seconds: 3 }).distance);
const spread = (Math.max(...distances) - Math.min(...distances)) / Math.max(...distances);
assert.ok(spread < 0.02,
  `distance varies ${(spread * 100).toFixed(1)}% across frame rates: ${distances.map((d) => d.toFixed(2))}`);

// Releasing the key stops the walker promptly but not dead.
const stop = run({ fps: 60, seconds: 1.0, holdFor: 0.5 });
assert.ok(stop.speed < 0.05, `still drifting after 0.5 s: ${stop.speed.toFixed(3)} m/s`);
const coastStart = stepWalkVelocity(topSpeed(1), 0, 0, 0, topSpeed(1), 1 / 60);
assert.ok(coastStart.x > 0 && coastStart.x < topSpeed(1), 'stopping must decay, not snap to zero');

// A long stall must not launch the walker across the model.
const stalled = stepWalkVelocity(0, 0, 0, 1, topSpeed(1), 5.0);
assert.ok(Math.hypot(stalled.x, stalled.z) <= topSpeed(1) + 1e-9, 'a stalled frame cannot exceed the pace');

// Diagonal input is normalised by the caller; walking at 45 degrees must be no
// faster than walking straight ahead.
let diag = { x: 0, z: 0 };
for (let i = 0; i < 120; i += 1) diag = stepWalkVelocity(diag.x, diag.z, Math.SQRT1_2, Math.SQRT1_2, topSpeed(1), 1 / 60);
close(Math.hypot(diag.x, diag.z), topSpeed(1), 1e-9, 'diagonal speed equals forward speed');

// ---------------------------------------------------------------- head bob
assert.deepEqual(gaitOffset(1.23, 0), { vertical: 0, lateral: 0 }, 'standing still is perfectly still');
assert.equal(gaitStrength(0), 0, 'no speed, no gait');
close(gaitStrength(WALK.baseSpeed), 1, 1e-9, 'walking pace is a full-strength gait');
assert.equal(gaitStrength(99), 1.6, 'the gait is capped so a sprint is not a pogo stick');
let maxV = 0;
let maxL = 0;
for (let phase = 0; phase < 20; phase += 0.01) {
  const o = gaitOffset(phase, gaitStrength(WALK.baseSpeed));
  maxV = Math.max(maxV, Math.abs(o.vertical));
  maxL = Math.max(maxL, Math.abs(o.lateral));
}
close(maxV, WALK.bobHeight, 1e-3, 'vertical head travel at a walk');
close(maxL, WALK.bobSway, 1e-3, 'lateral head travel at a walk');
assert.ok(maxV < 0.05 && maxL < 0.05, 'head movement must stay a cue, never a camera shake');
// The head rises and falls twice per stride and sways once.
close(gaitOffset(0, 1).vertical, 0, 1e-9, 'stride starts level');
close(gaitOffset(Math.PI, 1).vertical, 0, 1e-9, 'and is level again a stride later');
close(gaitOffset(Math.PI / 4, 1).vertical, WALK.bobHeight, 1e-9, 'peaking twice per stride');

console.log(`walk_check: OK — 1.85 m walker sees from ${eyeHeight(1.85).toFixed(2)} m at ${(topSpeed(1) * 3.6).toFixed(1)} km/h`);
