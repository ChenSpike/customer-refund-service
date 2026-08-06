CREATE TABLE IF NOT EXISTS policy_review_events (
  policy_review_event_id VARCHAR(36) NOT NULL,
  trace_id VARCHAR(36) NOT NULL,
  policy_version VARCHAR(50) NOT NULL,
  review_type VARCHAR(50) NOT NULL,
  policy_ids_json LONGTEXT NOT NULL,
  evidence_json LONGTEXT NOT NULL,
  detail LONGTEXT NOT NULL,
  created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (policy_review_event_id),
  KEY idx_policy_review_trace (trace_id),
  KEY idx_policy_review_type (review_type),
  CONSTRAINT fk_policy_review_trace FOREIGN KEY (trace_id) REFERENCES workflow_runs (trace_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

ALTER TABLE human_approvals
  ADD COLUMN policy_review_event_id VARCHAR(36) NULL AFTER triggering_event_id,
  ADD COLUMN approved_next_agent VARCHAR(255) NULL AFTER status,
  ADD COLUMN rejected_next_agent VARCHAR(255) NULL AFTER approved_next_agent,
  ADD KEY idx_human_policy_review (policy_review_event_id);

ALTER TABLE human_approvals
  ADD CONSTRAINT fk_human_policy_review
  FOREIGN KEY (policy_review_event_id) REFERENCES policy_review_events (policy_review_event_id);
