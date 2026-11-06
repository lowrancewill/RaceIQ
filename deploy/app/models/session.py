from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Float
from sqlalchemy import DateTime

from database import Base

class TelemetrySession(Base):

    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)

    filename = Column(String)

    upload_date = Column(DateTime)

    avg_speed = Column(Float)

    max_speed = Column(Float)

    consistency = Column(Float)

    driver_style = Column(String)