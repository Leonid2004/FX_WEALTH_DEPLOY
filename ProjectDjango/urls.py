"""
URL configuration for ProjectDjango project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from tracker.views import *
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path ('', HomeView.as_view(), name='home'),

    path("accounts/", include("django.contrib.auth.urls")),
    path("register/", register_view, name="register"),
    path("addposition/",add_position, name="addpos"),
    path("position/<int:pk>/", position_detail, name="position_detail"),
    path("projection/", portfolio_projection, name="portfolio_projection"),
    path("position/<int:pk>/edit/", edit_position, name="position_edit"),

]

