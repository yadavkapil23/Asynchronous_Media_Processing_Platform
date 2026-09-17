from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def list_uploads():
    return {"message": "Uploads endpoint working"}
