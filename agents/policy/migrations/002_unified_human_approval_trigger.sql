ALTER TABLE human_approvals
  ADD COLUMN triggering_event_type ENUM('governance', 'policy_review') NULL AFTER triggering_event_id;

ALTER TABLE human_approvals
  DROP FOREIGN KEY fk_human_approvals_event,
  DROP FOREIGN KEY fk_human_policy_review,
  DROP INDEX fk_human_approvals_event,
  DROP INDEX idx_human_policy_review;

UPDATE human_approvals
SET triggering_event_type = CASE
      WHEN policy_review_event_id IS NOT NULL THEN 'policy_review'
      ELSE 'governance'
    END,
    triggering_event_id = COALESCE(policy_review_event_id, triggering_event_id),
    updated_at = updated_at
WHERE triggering_event_id IS NOT NULL OR policy_review_event_id IS NOT NULL;

ALTER TABLE human_approvals
  DROP COLUMN policy_review_event_id,
  MODIFY triggering_event_id VARCHAR(36) NOT NULL,
  MODIFY triggering_event_type ENUM('governance', 'policy_review') NOT NULL,
  ADD KEY idx_human_approval_trigger (triggering_event_type, triggering_event_id);
