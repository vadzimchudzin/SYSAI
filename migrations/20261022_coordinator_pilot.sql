-- Additive only; review on a Neon development branch before production.
CREATE TABLE IF NOT EXISTS coordinator_actions (
	id SERIAL NOT NULL, 
	run_id VARCHAR(32) NOT NULL, 
	sequence INTEGER NOT NULL, 
	employee_id INTEGER NOT NULL, 
	recipient VARCHAR(40) NOT NULL, 
	body TEXT NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	message_id INTEGER, 
	artifact_id INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	attempted_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (run_id, sequence)
);

CREATE INDEX IF NOT EXISTS ix_coordinator_actions_run_id ON coordinator_actions (run_id);

CREATE INDEX IF NOT EXISTS ix_coordinator_actions_status ON coordinator_actions (status);

CREATE TABLE IF NOT EXISTS coordinator_events (
	id SERIAL NOT NULL, 
	run_id VARCHAR(32) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	data JSON NOT NULL, 
	external_key VARCHAR(100), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (external_key)
);

CREATE INDEX IF NOT EXISTS ix_coordinator_events_run_id ON coordinator_events (run_id);

CREATE TABLE IF NOT EXISTS coordinator_leases (
	name VARCHAR(40) NOT NULL, 
	token VARCHAR(32) NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (name)
);

CREATE TABLE IF NOT EXISTS coordinator_runs (
	id VARCHAR(32) NOT NULL, 
	task_id INTEGER NOT NULL, 
	owner_id INTEGER NOT NULL, 
	state VARCHAR(24) NOT NULL, 
	plan JSON NOT NULL, 
	summary TEXT NOT NULL, 
	reason VARCHAR(200) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_coordinator_runs_state ON coordinator_runs (state);

CREATE UNIQUE INDEX IF NOT EXISTS ix_coordinator_runs_task_id ON coordinator_runs (task_id);

