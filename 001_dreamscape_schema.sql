-- =====================================================================
-- DREAMSCAPE: Astrological & Spiritual Dream Tracking
-- Supabase / PostgreSQL 15+  |  Migration 001
-- =====================================================================
-- Design notes
--  * Sky data (ephemeris_*, environment_daily) is GLOBAL: one row set per
--    UTC day, shared by every user. Written only by the service role.
--  * Anything that depends on a person's chart or location (moon house,
--    planetary hours, synastry) lives in per-user tables.
--  * Each dream gets its own exact-time sky snapshot (dream_sky +
--    dream_transit_positions), because the Moon moves ~13 deg/day and a
--    once-a-day cache is too coarse for dream work.
--  * Synastry hits are calculated deterministically by the job (math),
--    while dream_analyses holds the AI interpretation layer on top.
-- =====================================================================

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------
-- ENUMS
-- ---------------------------------------------------------------------
create type chart_point as enum (
  'sun','moon','mercury','venus','mars','jupiter','saturn',
  'uranus','neptune','pluto','chiron','true_node',
  'asc','mc','dsc','ic'                     -- angles: natal only
);

create type zodiac_sign as enum (
  'aries','taurus','gemini','cancer','leo','virgo',
  'libra','scorpio','sagittarius','capricorn','aquarius','pisces'
);

create type aspect_type as enum ('conjunction','sextile','square','trine','opposition');

create type moon_phase_name as enum (
  'new_moon','waxing_crescent','first_quarter','waxing_gibbous',
  'full_moon','waning_gibbous','last_quarter','waning_crescent'
);

create type classical_element as enum ('fire','water','air','earth');

create type dream_threshold as enum (
  'mundane',                -- processing daily life
  'astral_psychological',   -- psychological mirror
  'spiritual_lucid'         -- transmission / visitation
);

create type transcript_status as enum ('none','pending','done','failed');

-- ---------------------------------------------------------------------
-- SHARED: updated_at trigger
-- ---------------------------------------------------------------------
create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

-- =====================================================================
-- 1. USER PROFILE + NATAL CHART
-- =====================================================================
create table public.profiles (
  user_id            uuid primary key references auth.users(id) on delete cascade,
  display_name       text,

  birth_date         date        not null,
  birth_time         time,                              -- null = unknown
  birth_time_known   boolean     not null default true, -- false = houses/angles unreliable
  birth_place_name   text,
  birth_lat          numeric(9,6) not null check (birth_lat between -90 and 90),
  birth_lon          numeric(9,6) not null check (birth_lon between -180 and 180),
  birth_tz           text        not null,              -- IANA, e.g. 'America/Detroit'

  -- where the user sleeps now (drives local dates + planetary hours)
  current_lat        numeric(9,6) not null check (current_lat between -90 and 90),
  current_lon        numeric(9,6) not null check (current_lon between -180 and 180),
  current_tz         text        not null,

  house_system       char(1)     not null default 'P',  -- Swiss Ephemeris code: P=Placidus, W=Whole Sign, K=Koch
  natal_chart        jsonb,                             -- full raw calculation output
  natal_computed_at  timestamptz,                       -- null => job will (re)compute

  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);
create trigger trg_profiles_touch before update on public.profiles
  for each row execute function public.touch_updated_at();

-- Changing birth data forces a natal recompute on the next job run
create or replace function public.reset_natal_on_birth_change()
returns trigger language plpgsql as $$
begin
  if (new.birth_date, new.birth_time, new.birth_lat, new.birth_lon, new.birth_tz, new.house_system)
     is distinct from
     (old.birth_date, old.birth_time, old.birth_lat, old.birth_lon, old.birth_tz, old.house_system) then
    new.natal_computed_at := null;
  end if;
  return new;
end $$;
create trigger trg_profiles_natal_reset before update on public.profiles
  for each row execute function public.reset_natal_on_birth_change();

-- Normalized natal placements (queryable copies of natal_chart)
create table public.natal_points (
  user_id         uuid        not null references public.profiles(user_id) on delete cascade,
  point           chart_point not null,
  longitude       numeric(9,6) not null check (longitude >= 0 and longitude < 360),
  sign            zodiac_sign not null,
  degree_in_sign  numeric(8,5) not null,
  house           smallint    check (house between 1 and 12),
  is_retrograde   boolean     not null default false,
  primary key (user_id, point)
);

create table public.natal_house_cusps (
  user_id    uuid     not null references public.profiles(user_id) on delete cascade,
  house      smallint not null check (house between 1 and 12),
  longitude  numeric(9,6) not null,
  sign       zodiac_sign not null,
  primary key (user_id, house)
);

create table public.natal_aspects (
  user_id   uuid        not null references public.profiles(user_id) on delete cascade,
  point_a   chart_point not null,
  point_b   chart_point not null,
  aspect    aspect_type not null,
  orb       numeric(5,3) not null,
  primary key (user_id, point_a, point_b, aspect),
  check (point_a < point_b)
);

-- =====================================================================
-- 2. DAILY COSMOLOGICAL EPHEMERIS CACHE (global, 00:00 UTC each day)
-- =====================================================================
create table public.ephemeris_daily (
  day                  date primary key,
  computed_for_utc     timestamptz not null,             -- the instant positions are calculated for
  sun_moon_elongation  numeric(7,3) not null,            -- 0-360
  moon_illumination    numeric(5,2) not null,            -- percent
  moon_phase           moon_phase_name not null,
  moon_longitude       numeric(9,6) not null,
  moon_sign            zodiac_sign not null,
  eclipse_type         text check (eclipse_type in ('solar','lunar')),  -- filled by analytics stage later
  engine               text not null default 'swisseph',
  engine_version       text,
  computed_at          timestamptz not null default now()
);

create table public.ephemeris_positions (
  day              date        not null references public.ephemeris_daily(day) on delete cascade,
  body             chart_point not null check (body not in ('asc','mc','dsc','ic')),
  longitude        numeric(9,6) not null,
  latitude         numeric(9,6),
  speed_deg_day    numeric(9,6) not null,
  sign             zodiac_sign not null,
  degree_in_sign   numeric(8,5) not null,
  is_retrograde    boolean not null,
  is_stationary    boolean not null default false,   -- |speed| below body threshold
  primary key (day, body)
);
create index idx_ephem_pos_retro on public.ephemeris_positions (body, day) where is_retrograde;

-- Geocentric aspects between transiting bodies, 5 deg orb
create table public.ephemeris_aspects (
  day         date        not null references public.ephemeris_daily(day) on delete cascade,
  body_a      chart_point not null,
  body_b      chart_point not null,
  aspect      aspect_type not null,
  orb         numeric(5,3) not null check (orb <= 5),
  is_applying boolean,
  primary key (day, body_a, body_b, aspect),
  check (body_a < body_b)
);

-- Per-user local-day context: Chaldean rulers + Moon's natal house
create table public.user_daily_context (
  user_id           uuid not null references public.profiles(user_id) on delete cascade,
  local_date        date not null,
  tz                text not null,
  sunrise           timestamptz,
  sunset            timestamptz,
  next_sunrise      timestamptz,
  day_ruler         chart_point not null,   -- ruler of 1st planetary hour after sunrise
  night_ruler       chart_point not null,   -- ruler of 1st planetary hour after sunset
  planetary_hours   jsonb,                  -- [{n,start,end,ruler,is_night}] x 24
  moon_sign_at_sunrise   zodiac_sign,
  moon_natal_house       smallint check (moon_natal_house between 1 and 12),
  computed_at       timestamptz not null default now(),
  primary key (user_id, local_date)
);

-- =====================================================================
-- 2b. ENVIRONMENTAL RESONANCE
-- =====================================================================
-- Automatic: NOAA SWPC (Kp = geomagnetic activity; flares = GOES X-ray class)
create table public.environment_daily (
  day               date primary key,             -- UTC day
  kp_max            numeric(3,2),
  kp_mean           numeric(3,2),
  kp_readings       jsonb,                        -- [{time_tag, kp}] 3-hourly
  g_storm_level     smallint check (g_storm_level between 0 and 5),  -- G0-G5 from kp_max
  flare_count       integer not null default 0,
  flare_max_class   text,                         -- e.g. 'M2.4', 'X1.1'
  flare_max_time    timestamptz,
  flares            jsonb,
  updated_at        timestamptz not null default now()
);

-- Manual: Schumann Resonance (no reliable numeric public feed exists)
create table public.schumann_readings (
  reading_id     uuid primary key default gen_random_uuid(),
  user_id        uuid not null references public.profiles(user_id) on delete cascade,
  observed_at    timestamptz not null default now(),
  frequency_hz   numeric(5,2) check (frequency_hz between 0 and 100),
  intensity_note text,           -- e.g. 'white bands across 14-40Hz'
  source_url     text,
  created_at     timestamptz not null default now()
);
create index idx_schumann_user_time on public.schumann_readings (user_id, observed_at desc);

-- =====================================================================
-- 3. SUBCONSCIOUS INGESTION LOGS (THE DREAM LOG)
-- =====================================================================
create table public.dream_logs (
  log_id               uuid primary key default gen_random_uuid(),
  user_id              uuid not null references public.profiles(user_id) on delete cascade,

  sleep_start          timestamptz,
  wake_time            timestamptz not null default now(),       -- creation timestamp
  sleep_duration       interval generated always as (wake_time - sleep_start) stored,
  local_date           date,                                     -- set by trigger from profile tz

  -- Stage 1: low-friction entry
  raw_text             text,
  voice_note_path      text,          -- key in storage bucket 'dream-voice': {user_id}/{log_id}.m4a
  voice_transcript     text,
  transcript_status    transcript_status not null default 'none',

  -- Stage 2: narrative / sensory extraction
  sensory_followup_q   text,          -- the one question the Guide asked
  sensory_details      text,          -- air, smell, temperature, dominant colors

  -- Stage 3: somatic check
  somatic_resonance    text[] not null default '{}',   -- body locations still holding the dream
  somatic_anchors      text[] not null default '{}',   -- sensations on waking: heavy_chest, vibrations, cold, third_eye_pressure

  -- Stage 4: spiritual threshold
  threshold_type       dream_threshold,

  -- Core metrics (1-10 sliders)
  lucid_clarity        smallint check (lucid_clarity between 1 and 10),
  vividness            smallint check (vividness between 1 and 10),
  emotional_intensity  smallint check (emotional_intensity between 1 and 10),
  spiritual_resonance  smallint check (spiritual_resonance between 1 and 10),
  recall_completeness  smallint check (recall_completeness between 1 and 10),

  primary_emotions     text[] not null default '{}',   -- dread, euphoria, serenity, confusion, nostalgia...

  guide_session        jsonb,          -- full Q/A transcript of the Dream Guide intake
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),

  check (raw_text is not null or voice_note_path is not null),
  check (sleep_start is null or sleep_start < wake_time)
);
create index idx_dream_logs_user_wake on public.dream_logs (user_id, wake_time desc);
create index idx_dream_logs_emotions  on public.dream_logs using gin (primary_emotions);
create index idx_dream_logs_somatic   on public.dream_logs using gin (somatic_anchors);
create trigger trg_dream_logs_touch before update on public.dream_logs
  for each row execute function public.touch_updated_at();

create or replace function public.set_dream_local_date()
returns trigger language plpgsql security definer set search_path = public as $$
declare v_tz text;
begin
  select current_tz into v_tz from public.profiles where user_id = new.user_id;
  new.local_date := (new.wake_time at time zone coalesce(v_tz, 'UTC'))::date;
  return new;
end $$;
create trigger trg_dream_logs_local_date before insert or update of wake_time on public.dream_logs
  for each row execute function public.set_dream_local_date();

-- Exact-time sky at wake (written by the job)
create table public.dream_sky (
  log_id              uuid primary key references public.dream_logs(log_id) on delete cascade,
  computed_for_utc    timestamptz not null,
  moon_phase          moon_phase_name not null,
  moon_illumination   numeric(5,2) not null,
  moon_sign           zodiac_sign not null,
  moon_natal_house    smallint check (moon_natal_house between 1 and 12),
  day_ruler           chart_point,
  planetary_hour_ruler chart_point,   -- ruler of the hour you woke in
  kp_at_wake          numeric(3,2),
  computed_at         timestamptz not null default now()
);

create table public.dream_transit_positions (
  log_id          uuid        not null references public.dream_logs(log_id) on delete cascade,
  body            chart_point not null,
  longitude       numeric(9,6) not null,
  sign            zodiac_sign not null,
  degree_in_sign  numeric(8,5) not null,
  is_retrograde   boolean not null,
  natal_house     smallint check (natal_house between 1 and 12),
  primary key (log_id, body)
);

-- Transit-to-natal contacts at wake time (deterministic)
create table public.dream_synastry_hits (
  hit_id            uuid primary key default gen_random_uuid(),
  log_id            uuid        not null references public.dream_logs(log_id) on delete cascade,
  transit_body      chart_point not null,
  natal_point       chart_point not null,
  aspect            aspect_type not null,
  orb               numeric(5,3) not null,
  is_applying       boolean,
  natal_point_house smallint,
  hits_8th_house    boolean not null default false,
  hits_12th_house   boolean not null default false,
  hits_natal_neptune boolean generated always as (natal_point = 'neptune') stored,
  unique (log_id, transit_body, natal_point, aspect)
);
create index idx_synastry_log on public.dream_synastry_hits (log_id);

-- =====================================================================
-- 4. META-ANALYSIS & INTERPRETATION OUTPUT
-- =====================================================================
create table public.dream_analyses (
  analysis_id              uuid primary key default gen_random_uuid(),
  log_id                   uuid not null references public.dream_logs(log_id) on delete cascade,
  is_current               boolean not null default true,
  model                    text not null,
  prompt_version           text not null,

  element_counts           jsonb,          -- {fire:n, water:n, air:n, earth:n}
  dominant_element         classical_element,
  elemental_synergy_index  numeric(5,3),   -- 0-1 overlap with transiting-sign elements

  elemental_metrics        jsonb,          -- full Step 1 output
  astrological_triggers    jsonb,          -- Step 2 (references dream_synastry_hits)
  archetypal_mapping       jsonb,          -- Step 3
  spiritual_synthesis      jsonb,          -- Step 4 + narrative
  settings                 text[] not null default '{}',

  raw_payload              jsonb not null, -- exact model response for audit / re-parse
  created_at               timestamptz not null default now()
);
create unique index uq_analysis_current on public.dream_analyses (log_id) where is_current;

-- Normalized extracted entities, for recurrence tracking across months
create table public.dream_entities (
  entity_id     uuid primary key default gen_random_uuid(),
  analysis_id   uuid not null references public.dream_analyses(analysis_id) on delete cascade,
  log_id        uuid not null references public.dream_logs(log_id) on delete cascade,
  user_id       uuid not null references public.profiles(user_id) on delete cascade,
  kind          text not null check (kind in ('archetype','entity','setting','symbol','person')),
  label         text not null,              -- 'grandmother', 'spiral staircase', 'the shadow'
  normalized    text generated always as (lower(trim(label))) stored,
  archetype     text,                       -- shadow, wise_guide, anima, animus, trickster, cosmic_mother
  ruling_planet chart_point,
  element       classical_element,
  confidence    numeric(3,2) check (confidence between 0 and 1)
);
create index idx_entities_recurrence on public.dream_entities (user_id, normalized);
create index idx_entities_archetype  on public.dream_entities (user_id, archetype);

-- =====================================================================
-- ROW LEVEL SECURITY
-- =====================================================================
alter table public.profiles            enable row level security;
alter table public.natal_points        enable row level security;
alter table public.natal_house_cusps   enable row level security;
alter table public.natal_aspects       enable row level security;
alter table public.user_daily_context  enable row level security;
alter table public.schumann_readings   enable row level security;
alter table public.dream_logs          enable row level security;
alter table public.dream_sky           enable row level security;
alter table public.dream_transit_positions enable row level security;
alter table public.dream_synastry_hits enable row level security;
alter table public.dream_analyses      enable row level security;
alter table public.dream_entities      enable row level security;
alter table public.ephemeris_daily     enable row level security;
alter table public.ephemeris_positions enable row level security;
alter table public.ephemeris_aspects   enable row level security;
alter table public.environment_daily   enable row level security;

-- Owner-only, read/write
create policy own_profile  on public.profiles           for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy own_schumann on public.schumann_readings  for all using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy own_dreams   on public.dream_logs         for all using (user_id = auth.uid()) with check (user_id = auth.uid());

-- Owner read-only (job writes these with the service role)
create policy read_natal_points on public.natal_points       for select using (user_id = auth.uid());
create policy read_natal_cusps  on public.natal_house_cusps  for select using (user_id = auth.uid());
create policy read_natal_aspects on public.natal_aspects     for select using (user_id = auth.uid());
create policy read_daily_ctx    on public.user_daily_context for select using (user_id = auth.uid());
create policy read_entities     on public.dream_entities     for select using (user_id = auth.uid());

create policy read_dream_sky on public.dream_sky for select
  using (exists (select 1 from public.dream_logs d where d.log_id = dream_sky.log_id and d.user_id = auth.uid()));
create policy read_dream_transits on public.dream_transit_positions for select
  using (exists (select 1 from public.dream_logs d where d.log_id = dream_transit_positions.log_id and d.user_id = auth.uid()));
create policy read_dream_synastry on public.dream_synastry_hits for select
  using (exists (select 1 from public.dream_logs d where d.log_id = dream_synastry_hits.log_id and d.user_id = auth.uid()));
create policy read_dream_analyses on public.dream_analyses for select
  using (exists (select 1 from public.dream_logs d where d.log_id = dream_analyses.log_id and d.user_id = auth.uid()));

-- Global sky data: any signed-in user can read
create policy read_ephem_daily on public.ephemeris_daily     for select to authenticated using (true);
create policy read_ephem_pos   on public.ephemeris_positions for select to authenticated using (true);
create policy read_ephem_asp   on public.ephemeris_aspects   for select to authenticated using (true);
create policy read_environment on public.environment_daily   for select to authenticated using (true);

-- =====================================================================
-- STORAGE: private voice-note bucket, one folder per user
-- =====================================================================
insert into storage.buckets (id, name, public)
values ('dream-voice', 'dream-voice', false)
on conflict (id) do nothing;

create policy voice_owner_rw on storage.objects for all to authenticated
  using      (bucket_id = 'dream-voice' and (storage.foldername(name))[1] = auth.uid()::text)
  with check (bucket_id = 'dream-voice' and (storage.foldername(name))[1] = auth.uid()::text);
