/* 3D OSM Model — walk-mode body model.
 *
 * Walk mode is meant to be lived from inside a body, not flown as a low camera.
 * Every number here is anthropometric or gait data rather than a game-feel knob,
 * and everything vertical derives from a single setting — the walker's stature,
 * 1.85 m by default:
 *
 *   standing eye height = 0.936 x stature   (Pheasant, *Bodyspace*)
 *   crouched eye height = 0.60  x stature
 *   walking pace        = 1.8 m/s           (brisk; the 1.4 m/s textbook mean
 *                                            read as sluggish in the viewer)
 *   cadence             = one step per 0.78 m travelled
 *
 * Before this the camera accelerated to 9.2 m/s — 33 km/h, a car in a city —
 * and passed straight through walls, which is why the model never read as a
 * place you could stand in.
 *
 * The velocity integrator lives here, free of three.js, because its one real
 * failure mode is silent: a step that is not frame-rate independent still looks
 * fine on the developer's machine and walks at a different speed on everyone
 * else's. tests/walk_check.mjs pins that down.
 */

export const WALK = {
  eyeRatio: 0.936,
  crouchRatio: 0.60,
  bodyRadius: 0.34,      // shoulder half-width, used for wall collision
  baseSpeed: 1.8,        // m/s at pace 1.0 — a brisk walk (6.5 km/h)
  sprintFactor: 2.0,     // -> 3.6 m/s, a run
  crouchFactor: 0.5,
  accel: 8.0,            // m/s^2: walking pace reached in about 0.18 s
  decel: 11.0,           // stopping is quicker than starting — you plant a foot
  stepLength: 0.78,      // m per step; sets the head-bob cadence
  bobHeight: 0.013,      // m the head rises and falls, twice per stride
  bobSway: 0.009,        // m the head sways sideways, once per stride
  fov: 72,               // walk field of view; the orbit FOV is restored on exit
  dropInHeight: 25,      // above this, entering walk mode lands you at the look-at point
  defaultStature: 1.85,
  minStature: 1.2,
  maxStature: 2.2,
  minPace: 0.2,
  maxPace: 2.5
};

/* Eye height for a body of this stature. The camera sits at the eyes, not at
 * the top of the head — a 1.85 m walker looks out from 1.73 m. */
export function eyeHeight(stature, crouched = false) {
  // Absent or nonsensical -> the default body. A real but out-of-range value is
  // clamped rather than replaced, so dragging the slider to an end stops there
  // instead of snapping back to 1.85.
  const raw = Number(stature);
  const s = Number.isFinite(raw) && raw > 0
    ? Math.min(WALK.maxStature, Math.max(WALK.minStature, raw))
    : WALK.defaultStature;
  return s * (crouched ? WALK.crouchRatio : WALK.eyeRatio);
}

export function topSpeed(pace, { sprinting = false, crouching = false } = {}) {
  const raw = Number(pace);
  const p = Number.isFinite(raw) ? Math.min(WALK.maxPace, Math.max(WALK.minPace, raw)) : 1;
  return WALK.baseSpeed * p
    * (sprinting ? WALK.sprintFactor : 1)
    * (crouching ? WALK.crouchFactor : 1);
}

/* One integration step of the walker's horizontal velocity, in the camera's own
 * axes and keeping the viewer's sign convention (input drives velocity negative;
 * the caller hands the result to moveRight/moveForward negated).
 *
 * The velocity ramps *towards* the pace the body wants rather than being pushed
 * by a force that friction then fights. The earlier "constant force minus
 * friction" form settled wherever accel/friction happened to land — 1.077 m/s
 * with the tuning above, so the walker silently never reached the 1.4 m/s the
 * clamp promised, and the clamp itself never once applied. A bounded ramp is
 * also exactly frame-rate independent, which the damped form only approximated.
 *
 * `delta` is clamped: a stalled tab must not teleport the walker across the model
 * when it resumes. */
export function stepWalkVelocity(vx, vz, inputX, inputZ, top, delta) {
  const dt = Math.min(Math.max(Number(delta) || 0, 0), 0.1);
  const wantX = -inputX * top;
  const wantZ = -inputZ * top;
  const gapX = wantX - vx;
  const gapZ = wantZ - vz;
  const gap = Math.hypot(gapX, gapZ);
  const moving = inputX !== 0 || inputZ !== 0;
  const rate = (moving ? WALK.accel : WALK.decel) * dt;
  let x;
  let z;
  if (gap <= rate || gap === 0) {
    x = wantX;
    z = wantZ;
  } else {
    x = vx + (gapX / gap) * rate;
    z = vz + (gapZ / gap) * rate;
  }
  return { x, z, speed: Math.hypot(x, z) };
}

/* Head movement for the current stride phase. Vertical rises and falls twice per
 * stride, lateral sways once — both scaled by how fast the body is actually
 * travelling, so standing still is perfectly still. */
export function gaitOffset(phase, gait) {
  if (!(gait > 0)) return { vertical: 0, lateral: 0 };
  return {
    vertical: Math.sin(phase * 2) * WALK.bobHeight * gait,
    lateral: Math.cos(phase) * WALK.bobSway * gait
  };
}

/* How strongly the gait shows, from the speed the body is actually making.
 * Capped so a sprint does not turn into a pogo stick. */
export function gaitStrength(speed) {
  return Math.min(1.6, Math.max(0, speed / WALK.baseSpeed));
}
