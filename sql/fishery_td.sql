CREATE DATABASE IF NOT EXISTS fishery KEEP 365;
USE fishery;
CREATE STABLE IF NOT EXISTS sensor_data (ts TIMESTAMP, temperature FLOAT, ph_value FLOAT, dissolved_oxygen FLOAT, ammonia_nitrogen FLOAT, nitrite FLOAT, ts_value FLOAT, water_level FLOAT) TAGS (pond_id BINARY(50), device_id BINARY(50), product_id BINARY(50));
CREATE TABLE IF NOT EXISTS alerts (ts TIMESTAMP, pond_id BINARY(50), device_id BINARY(50), alert_type BINARY(50), alert_value FLOAT, threshold_value FLOAT, message BINARY(255), is_read BOOL);
CREATE TABLE IF NOT EXISTS device_status (ts TIMESTAMP, device_id BINARY(50), status TINYINT, login_type TINYINT);
CREATE TABLE IF NOT EXISTS alert_duration (ts TIMESTAMP, pond_id BINARY(50), device_id BINARY(50), alert_type BINARY(50), alert_value FLOAT, threshold_value FLOAT, started_at TIMESTAMP, last_checked_at TIMESTAMP, duration_minutes INT, status BINARY(20), agent_decision BINARY(500), agent_action BINARY(100));
CREATE TABLE IF NOT EXISTS device_commands (ts TIMESTAMP, pond_id BINARY(50), device_id BINARY(50), command_type BINARY(50), trigger_source BINARY(50), trigger_alert_id BINARY(50), params BINARY(500), status BINARY(20), executed_at TIMESTAMP, result BINARY(500));