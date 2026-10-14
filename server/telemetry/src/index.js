// Receives the opt-in usage ping from modules/telemetry.py. This file is all
// of the server logic.
//
// Stored per install per UTC day: the payload's fields plus the 2-letter
// country Cloudflare derives from the connection. The IP address, headers
// and user agent are not stored. Anything that isn't exactly the documented
// payload is rejected.

const COUNTERS = ['dictations', 'meeting_sessions', 'phone_sessions', 'failures', 'minutes'];
const KEYS = new Set(['v', 'id', 'app', 'build', 'provider', ...COUNTERS]);
const MAX_PER_DAY = 5000; // caps what a single ID can add in a day
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function valid(p) {
  if (!p || typeof p !== 'object' || Array.isArray(p)) return false;
  const keys = Object.keys(p);
  if (keys.length !== KEYS.size || !keys.every((k) => KEYS.has(k))) return false;
  if (p.v !== 1 || typeof p.id !== 'string' || !UUID.test(p.id)) return false;
  if (typeof p.app !== 'string' || !/^[0-9A-Za-z.+-]{1,20}$/.test(p.app)) return false;
  if (p.build !== 'installer' && p.build !== 'source') return false;
  if (typeof p.provider !== 'string' || !/^[a-z0-9_-]{0,20}$/.test(p.provider)) return false;
  return COUNTERS.every((k) => Number.isInteger(p[k]) && p[k] >= 0 && p[k] <= MAX_PER_DAY);
}

async function ping(request, env) {
  const body = await request.text();
  if (body.length > 1024) return new Response(null, { status: 413 });
  let p;
  try {
    p = JSON.parse(body);
  } catch {
    return new Response(null, { status: 400 });
  }
  if (!valid(p)) return new Response(null, { status: 400 });

  const day = new Date().toISOString().slice(0, 10);
  const country = (request.cf && request.cf.country) || null;
  const sums = COUNTERS.map((k) => `${k} = MIN(${k} + excluded.${k}, ${MAX_PER_DAY})`).join(', ');
  await env.DB.prepare(
    `INSERT INTO pings (day, id, app, build, provider, country, ${COUNTERS.join(', ')}, pings)
     VALUES (?, ?, ?, ?, ?, ?, ${COUNTERS.map(() => '?').join(', ')}, 1)
     ON CONFLICT (day, id) DO UPDATE SET
       app = excluded.app, build = excluded.build, provider = excluded.provider,
       country = excluded.country, ${sums}, pings = pings + 1`
  ).bind(day, p.id, p.app, p.build, p.provider, country, ...COUNTERS.map((k) => p[k])).run();
  return new Response(null, { status: 204 });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/v1/ping' && request.method === 'POST') return ping(request, env);
    return new Response('Better Voice Typing usage counts. Source: '
      + 'https://github.com/Elevate-Code/better-voice-typing/tree/master/server/telemetry\n',
      { status: url.pathname === '/' ? 200 : 404 });
  },
};
