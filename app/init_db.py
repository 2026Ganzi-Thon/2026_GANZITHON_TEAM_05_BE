"""One-time local bootstrap. For Supabase use sql/001_schema.sql in SQL Editor instead."""
from app.database import Base, engine
import app.models  # register all 8 models

if __name__ == '__main__':
    Base.metadata.create_all(bind=engine)
    print('Database schema created: 8 tables')
