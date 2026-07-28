"""Jinja2 template engine for email replies"""
from jinja2 import Environment, FileSystemLoader
from pathlib import Path

TEMPLATE_DIR = Path('/opt/edm-system/templates')

class TemplateEngine:
    def __init__(self):
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=True
        )
    
    def render(self, template_name, **context):
        """Render email template with context"""
        template = self.env.get_template(template_name)
        return template.render(**context)
    
    def list_templates(self):
        """List available templates"""
        return self.env.list_templates()
