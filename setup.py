from setuptools import setup, find_packages

setup(
    name="seo-crawler-suite",
    version="1.0.0",
    description="SEO crawler and competitor keyword-gap engine with an MCP server and CLI.",
    py_modules=["cli", "seo_crawler", "seo_mcp_server", "lan_http"],
    packages=find_packages(),
    install_requires=[
        "requests",
        "beautifulsoup4"
    ],
    extras_require={
        "mcp": ["mcp[cli]<2"],
    },
    entry_points={
        "console_scripts": [
            "seo-crawler=cli:main",
            "seo-mcp-server=seo_mcp_server:main",
        ]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
        "License :: OSI Approved :: MIT License",
        "Topic :: Internet :: WWW/HTTP :: Indexing/Search",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Utilities"
    ],
    python_requires=">=3.10",
)
