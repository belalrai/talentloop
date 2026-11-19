-- Optional: the app creates these automatically on first request. Run in Supabase > SQL Editor if you prefer.
CREATE TABLE IF NOT EXISTS candidates(id SERIAL PRIMARY KEY, name TEXT, email TEXT, phone TEXT, role TEXT, location TEXT, language TEXT, data TEXT, source TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS templates(id SERIAL PRIMARY KEY, name TEXT, subject TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS messages(id SERIAL PRIMARY KEY, cid INTEGER, channel TEXT, tpl INTEGER, job TEXT, status TEXT, ts DOUBLE PRECISION);
ALTER TABLE candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
