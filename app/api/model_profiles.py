from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import current_user
from app.models.entities import UserAccount
from app.services.model_profiles import ModelProfileService, ProfileInput, ProfileError, ProfileConflict, catalog


router = APIRouter(prefix='/api/model-profile', tags=['model-profile'])


def service(db):
    return ModelProfileService(db, get_settings())


def checked(action):
    try:
        return action()
    except ProfileConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ProfileError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get('')
def profile(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    return checked(lambda: service(db).public(user.id))


@router.get('/connections')
def available_connections(_: Annotated[UserAccount, Depends(current_user)]):
    return checked(lambda: catalog(get_settings()))


@router.post('/test')
def test_connection(body: ProfileInput, user: Annotated[UserAccount, Depends(current_user)],
                    db: Annotated[Session, Depends(get_db)]):
    return checked(lambda: service(db).test(user.id, body)[2])


@router.put('')
def save_profile(body: ProfileInput, user: Annotated[UserAccount, Depends(current_user)],
                 db: Annotated[Session, Depends(get_db)]):
    return checked(lambda: service(db).save(user.id, body))


@router.delete('')
def reset_profile(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)],
                  revision: Annotated[int, Query(ge=1)]):
    return checked(lambda: service(db).reset(user.id, revision))
