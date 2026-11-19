-- Letit initial PostgreSQL schema. Execute in Supabase SQL Editor.

BEGIN;

CREATE TABLE IF NOT EXISTS users (
	id UUID NOT NULL, 
	username VARCHAR(80) NOT NULL, 
	password_hash TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS daily_records (
	user_id UUID NOT NULL, 
	record_date DATE NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	ever_completed BOOLEAN NOT NULL, 
	PRIMARY KEY (user_id, record_date), 
	CONSTRAINT valid_daily_status CHECK (status IN ('completed','partial','rest','undecided')), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS recommendations (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	title TEXT NOT NULL, 
	description TEXT NOT NULL, 
	energy_level INTEGER NOT NULL, 
	mission_type VARCHAR(20) NOT NULL, 
	place_category VARCHAR(50), 
	steps JSONB NOT NULL, 
	conditions JSONB NOT NULL, 
	estimated_duration_minutes INTEGER NOT NULL, 
	estimated_cost INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT valid_recommendation_type CHECK (mission_type IN ('place','non_place')), 
	CONSTRAINT valid_recommendation_energy CHECK (energy_level BETWEEN 0 AND 4), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS user_defaults (
	user_id UUID NOT NULL, 
	mood VARCHAR(30), 
	energy_level INTEGER NOT NULL, 
	barriers JSONB NOT NULL, 
	distance_limit VARCHAR(10) NOT NULL, 
	time_limit VARCHAR(10) NOT NULL, 
	budget_limit INTEGER, 
	onboarding_completed BOOLEAN NOT NULL, 
	PRIMARY KEY (user_id), 
	CONSTRAINT valid_defaults_energy CHECK (energy_level BETWEEN 0 AND 4), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_flowers (
	user_id UUID NOT NULL, 
	flower_number INTEGER NOT NULL, 
	acquired_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	claimed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (user_id, flower_number), 
	CONSTRAINT valid_flower_number CHECK (flower_number BETWEEN 1 AND 18), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS missions (
	id UUID NOT NULL, 
	user_id UUID NOT NULL, 
	recommendation_id UUID, 
	title TEXT NOT NULL, 
	description TEXT NOT NULL, 
	energy_level INTEGER NOT NULL, 
	mission_type VARCHAR(20) NOT NULL, 
	place_category VARCHAR(50), 
	status VARCHAR(20) NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	estimated_duration_minutes INTEGER NOT NULL, 
	estimated_cost INTEGER NOT NULL, 
	place_info JSONB, 
	archived_hidden BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT valid_mission_status CHECK (status IN ('in_progress','paused','completed','abandoned','replaced')), 
	CONSTRAINT valid_mission_type CHECK (mission_type IN ('place','non_place')), 
	CONSTRAINT valid_mission_energy CHECK (energy_level BETWEEN 0 AND 4), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(recommendation_id) REFERENCES recommendations (id)
);

CREATE TABLE IF NOT EXISTS mission_reviews (
	mission_id UUID NOT NULL, 
	mood_after VARCHAR(30), 
	one_line TEXT, 
	retry_activity TEXT, 
	good_point TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (mission_id), 
	FOREIGN KEY(mission_id) REFERENCES missions (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS mission_steps (
	id UUID NOT NULL, 
	mission_id UUID NOT NULL, 
	step_order INTEGER NOT NULL, 
	text TEXT NOT NULL, 
	is_completed BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (mission_id, step_order), 
	CONSTRAINT valid_step_order CHECK (step_order BETWEEN 1 AND 5), 
	FOREIGN KEY(mission_id) REFERENCES missions (id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username);

CREATE INDEX IF NOT EXISTS ix_recommendations_user_id ON recommendations (user_id);

CREATE INDEX IF NOT EXISTS ix_missions_user_id ON missions (user_id);

CREATE INDEX IF NOT EXISTS ix_mission_steps_mission_id ON mission_steps (mission_id);

-- Security: block direct public/anon Data API access; this app accesses tables only via its FastAPI backend.
-- Do not create broad anon/authenticated policies unless you deliberately change the architecture.
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_defaults ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recommendations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.missions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mission_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.mission_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_flowers ENABLE ROW LEVEL SECURITY;

COMMIT;
