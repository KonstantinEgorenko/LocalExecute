from .models import ResearchCapability

CAPABILITY_REQUIREMENTS = {
    ResearchCapability.WEB_BROWSER_RESEARCH: {
        "authority": "READ_ONLY_EXTERNAL",
        "provider": "phase20a_browser",
        "requires_gpu": False,
        "llm_handoff_authority": False,
    },
    ResearchCapability.FILE_RESEARCH: {
        "authority": "READ_ONLY_ARTIFACT",
        "provider": "phase20a_file",
        "requires_gpu": False,
        "llm_handoff_authority": False,
    },
    ResearchCapability.MEDIA_RESEARCH: {
        "authority": "READ_ONLY_ARTIFACT",
        "provider": "phase20a_media",
        "requires_gpu": False,
        "llm_handoff_authority": False,
    },
}
